#!/usr/bin/env python3
"""
SafeEye CLI — full command-line interface for the SafeEye Content Safety Scanner.

Usage:
    python -m nsfw_scanner.cli [command] [options]

Config:
    Set SAFEEYE_URL and SAFEEYE_TOKEN as environment variables,
    or create ~/.safeeye with:
        url = https://your-server:1985
        token = your-api-token
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ──────────────────────────────────────────────
# ANSI colour helpers
# ──────────────────────────────────────────────

_NO_COLOR = os.environ.get("NO_COLOR") or not sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def red(t: str) -> str:    return _c("31", t)
def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def blue(t: str) -> str:   return _c("34", t)
def cyan(t: str) -> str:   return _c("36", t)
def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

_CONFIG_FILE = Path.home() / ".safeeye"

def _load_config() -> dict:
    """Load config from env vars, falling back to ~/.safeeye file."""
    cfg = {"url": "", "token": ""}

    # File config (lowest priority)
    if _CONFIG_FILE.exists():
        try:
            for line in _CONFIG_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    cfg[key.strip().lower()] = val.strip()
        except OSError:
            pass

    # Environment overrides
    if os.environ.get("SAFEEYE_URL"):
        cfg["url"] = os.environ["SAFEEYE_URL"]
    if os.environ.get("SAFEEYE_TOKEN"):
        cfg["token"] = os.environ["SAFEEYE_TOKEN"]

    # Normalise
    cfg["url"] = cfg["url"].rstrip("/")
    return cfg


def _cfg_url() -> str:
    url = _load_config()["url"]
    if not url:
        _die("No server URL configured. Set SAFEEYE_URL or add 'url = ...' to ~/.safeeye")
    return url


def _cfg_token() -> str:
    token = _load_config()["token"]
    if not token:
        _die("No API token configured. Set SAFEEYE_TOKEN or add 'token = ...' to ~/.safeeye")
    return token


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

_QUIET = False
_JSON_MODE = False


def _die(msg: str, code: int = 1):
    if _JSON_MODE:
        print(json.dumps({"error": msg}))
    else:
        print(red(f"Error: {msg}"), file=sys.stderr)
    sys.exit(code)


def _info(msg: str):
    if not _QUIET and not _JSON_MODE:
        print(msg)


def _success(msg: str):
    if not _QUIET and not _JSON_MODE:
        print(green(msg))


def _warn(msg: str):
    if not _QUIET and not _JSON_MODE:
        print(yellow(msg), file=sys.stderr)


def _print_json(data, force: bool = False):
    """Pretty-print JSON data — coloured for TTY, raw with --json."""
    if _JSON_MODE or force:
        print(json.dumps(data, indent=2, default=str))
        return
    if _QUIET:
        return
    _pretty(data, indent=0)


def _pretty(obj, indent: int = 0):
    """Recursively pretty-print a dict/list with ANSI colours."""
    pad = "  " * indent
    if isinstance(obj, dict):
        for key, val in obj.items():
            label = cyan(str(key))
            if isinstance(val, (dict, list)):
                print(f"{pad}{label}:")
                _pretty(val, indent + 1)
            else:
                print(f"{pad}{label}: {_format_val(val)}")
    elif isinstance(obj, list):
        if not obj:
            print(f"{pad}{dim('(empty)')}")
            return
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                print(f"{pad}{dim(f'[{i}]')}")
                _pretty(item, indent + 1)
            else:
                print(f"{pad}- {_format_val(item)}")
    else:
        print(f"{pad}{_format_val(obj)}")


def _format_val(val) -> str:
    if val is True:
        return green("true")
    if val is False:
        return red("false")
    if val is None:
        return dim("null")
    if isinstance(val, (int, float)):
        return yellow(str(val))
    return str(val)


# ──────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────

def _headers(master: bool = False) -> dict:
    return {"Authorization": f"Bearer {_cfg_token()}"}


def _get(path: str, params: dict | None = None, master: bool = False) -> dict:
    url = f"{_cfg_url()}{path}"
    try:
        r = requests.get(url, headers=_headers(master), params=params, timeout=60)
    except requests.ConnectionError:
        _die(f"Cannot connect to {_cfg_url()}")
    except requests.Timeout:
        _die("Request timed out")
    if r.status_code >= 400:
        _handle_error(r)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _post(path: str, json_body: dict | None = None, files: dict | None = None,
          params: dict | None = None, master: bool = False) -> dict:
    url = f"{_cfg_url()}{path}"
    try:
        r = requests.post(url, headers=_headers(master), json=json_body,
                          files=files, params=params, timeout=120)
    except requests.ConnectionError:
        _die(f"Cannot connect to {_cfg_url()}")
    except requests.Timeout:
        _die("Request timed out")
    if r.status_code >= 400:
        _handle_error(r)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _delete(path: str, master: bool = False) -> dict:
    url = f"{_cfg_url()}{path}"
    try:
        r = requests.delete(url, headers=_headers(master), timeout=30)
    except requests.ConnectionError:
        _die(f"Cannot connect to {_cfg_url()}")
    except requests.Timeout:
        _die("Request timed out")
    if r.status_code >= 400:
        _handle_error(r)
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _get_raw(path: str, params: dict | None = None) -> requests.Response:
    """Return the raw Response object (used for CSV export etc.)."""
    url = f"{_cfg_url()}{path}"
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=60)
    except requests.ConnectionError:
        _die(f"Cannot connect to {_cfg_url()}")
    except requests.Timeout:
        _die("Request timed out")
    if r.status_code >= 400:
        _handle_error(r)
    return r


def _handle_error(r: requests.Response):
    try:
        body = r.json()
        detail = body.get("detail", body)
    except ValueError:
        detail = r.text[:300]
    _die(f"HTTP {r.status_code}: {detail}")


# ──────────────────────────────────────────────
# Progress spinner for async polling
# ──────────────────────────────────────────────

_SPINNER = ["|", "/", "-", "\\"]

def _poll_job(job_id: str, label: str = "Processing") -> dict:
    """Poll a job until it completes, showing a spinner."""
    idx = 0
    while True:
        data = _get(f"/api/v1/job/{job_id}")
        status = data.get("status", "unknown")
        if status in ("completed", "failed"):
            if not _QUIET and not _JSON_MODE:
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()
            return data
        if not _QUIET and not _JSON_MODE:
            sym = _SPINNER[idx % len(_SPINNER)]
            sys.stdout.write(f"\r  {sym} {label} [{status}]...")
            sys.stdout.flush()
        idx += 1
        time.sleep(1)


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────

def cmd_scan(args):
    """Scan a file or URL."""
    targets = args.targets

    # -- batch mode --
    if args.batch:
        if not targets:
            _die("Provide URLs for batch scanning")
        body = {"urls": targets}
        if args.webhook:
            body["webhook_url"] = args.webhook
        data = _post("/api/v1/scan/batch", json_body=body)
        batch_id = data.get("batch_id", "")
        _info(f"Batch submitted: {bold(batch_id)} ({data.get('total', 0)} URLs)")

        if not args.no_wait:
            _info("Polling for results...")
            time.sleep(1)
            while True:
                bd = _get(f"/api/v1/batch/{batch_id}")
                done = bd.get("completed", 0) + bd.get("failed", 0)
                total = bd.get("total", len(targets))
                if not _QUIET and not _JSON_MODE:
                    sys.stdout.write(f"\r  Progress: {done}/{total}")
                    sys.stdout.flush()
                if done >= total:
                    if not _QUIET and not _JSON_MODE:
                        sys.stdout.write("\n")
                    _print_json(bd)
                    return
                time.sleep(2)
        else:
            _print_json(data)
        return

    # -- single target --
    if not targets:
        _die("Provide a file path or URL to scan")
    target = targets[0]

    # -- async mode --
    if args.async_mode:
        params = {"url": target}
        if args.webhook:
            params["webhook_url"] = args.webhook
        data = _post("/api/v1/scan/async", params=params)
        job_id = data.get("job_id", "")
        _info(f"Job submitted: {bold(job_id)}")

        if not args.no_wait:
            result = _poll_job(job_id)
            _print_json(result)
        else:
            _print_json(data)
        return

    # -- synchronous scan --
    is_file = os.path.isfile(target)

    if is_file:
        _info(f"Scanning file: {bold(target)}")
        with open(target, "rb") as f:
            files = {"file": (os.path.basename(target), f)}
            data = _post("/api/v1/scan/file", files=files)
    else:
        # Treat as URL
        _info(f"Scanning URL: {bold(target)}")
        data = _post("/api/v1/scan/url", params={"url": target})

    # Highlight NSFW result
    result = data.get("result", data)
    if not _JSON_MODE and not _QUIET:
        is_nsfw = result.get("is_nsfw", False) if isinstance(result, dict) else False
        confidence = result.get("confidence", 0) if isinstance(result, dict) else 0
        if is_nsfw:
            print(red(bold(f"  NSFW DETECTED  (confidence: {confidence:.1%})")))
        else:
            print(green(bold(f"  SAFE  (confidence: {confidence:.1%})")))
        print()

    _print_json(data)


def cmd_job(args):
    """Check async job status."""
    data = _get(f"/api/v1/job/{args.job_id}")
    _print_json(data)


def cmd_batch(args):
    """Check batch status."""
    data = _get(f"/api/v1/batch/{args.batch_id}")
    _print_json(data)


# -- Token management --

def cmd_tokens(args):
    action = args.action

    if action == "list":
        data = _get("/api/v1/admin/tokens")
        if not data:
            _info("No tokens found.")
            return
        if _JSON_MODE:
            _print_json(data)
            return
        # Table display
        print()
        print(f"  {bold('Name'):<30} {bold('Created'):<22} {bold('Expires'):<22} {bold('Scans')}")
        print(f"  {'─' * 30} {'─' * 22} {'─' * 22} {'─' * 8}")
        items = data if isinstance(data, list) else [data]
        for t in items:
            name = t.get("name", "?")
            created = t.get("created_at", "?")[:19]
            expires = t.get("expires_at") or dim("never")
            if isinstance(expires, str) and len(expires) > 19:
                expires = expires[:19]
            scans = t.get("scan_count", 0)
            print(f"  {cyan(name):<39} {created:<22} {expires:<31} {yellow(str(scans))}")
        print()

    elif action == "create":
        if not args.name:
            _die("Provide a token name: safeeye tokens create <name>")
        body = {"name": args.name}
        if args.expires:
            body["expires_in_days"] = args.expires
        data = _post("/api/v1/admin/tokens", json_body=body)
        token = data.get("token", "")
        _success(f"Token created: {args.name}")
        if not _JSON_MODE:
            print()
            print(f"  {bold('Token')}: {yellow(token)}")
            print(f"  {dim('Save this token — it cannot be retrieved later.')}")
            print()
        else:
            _print_json(data)

    elif action == "revoke":
        if not args.name:
            _die("Provide a token name: safeeye tokens revoke <name>")
        data = _delete(f"/api/v1/admin/tokens/{args.name}")
        _success(f"Token revoked: {args.name}")

    elif action == "rotate":
        if not args.name:
            _die("Provide a token name: safeeye tokens rotate <name>")
        data = _post(f"/api/v1/admin/tokens/{args.name}/rotate")
        new_token = data.get("new_token", "")
        _success(f"Token rotated: {args.name}")
        if not _JSON_MODE:
            print()
            print(f"  {bold('New Token')}: {yellow(new_token)}")
            print(f"  {dim('Old token remains valid for 24 hours.')}")
            print()
        else:
            _print_json(data)

    else:
        _die(f"Unknown tokens action: {action}. Use: list, create, revoke, rotate")


# -- Stats --

def cmd_stats(args):
    sub = args.sub

    if sub == "providers":
        data = _get("/api/v1/stats/providers")
        _print_json(data)

    elif sub == "history":
        params = {"limit": args.limit, "offset": args.offset}
        if args.nsfw_only:
            params["nsfw_only"] = "true"
        data = _get("/api/v1/stats/history", params=params)
        if _JSON_MODE:
            _print_json(data)
            return
        items = data if isinstance(data, list) else []
        if not items:
            _info("No scan history.")
            return
        print()
        print(f"  {bold('Scan ID'):<20} {bold('Time'):<22} {bold('NSFW'):<8} {bold('Confidence'):<12} {bold('Labels')}")
        print(f"  {'─' * 20} {'─' * 22} {'─' * 8} {'─' * 12} {'─' * 30}")
        for s in items:
            sid = str(s.get("scan_id", s.get("id", "?")))[:18]
            ts = str(s.get("timestamp", "?"))[:19]
            nsfw = s.get("is_nsfw", False)
            nsfw_str = red("YES") if nsfw else green("no")
            conf = s.get("confidence", 0)
            labels = s.get("labels", [])
            if isinstance(labels, str):
                try:
                    labels = json.loads(labels)
                except (json.JSONDecodeError, TypeError):
                    labels = [labels]
            label_str = ", ".join(labels) if labels else dim("-")
            print(f"  {sid:<20} {ts:<22} {nsfw_str:<17} {yellow(f'{conf:.1%}'):<21} {label_str}")
        print()

    elif sub == "export":
        fmt = args.format or "json"
        r = _get_raw("/api/v1/stats/export", params={"format": fmt})
        if args.output:
            Path(args.output).write_bytes(r.content)
            _success(f"Exported to {args.output}")
        else:
            if fmt == "csv":
                print(r.text)
            else:
                try:
                    _print_json(r.json())
                except ValueError:
                    print(r.text)
    else:
        # Default: overview
        data = _get("/api/v1/stats")
        _print_json(data)


# -- Providers --

def cmd_providers(args):
    data = _get("/api/v1/admin/providers")
    if _JSON_MODE:
        _print_json(data)
        return
    print()
    print(f"  {bold('Provider'):<20} {bold('Type'):<10} {bold('Status')}")
    print(f"  {'─' * 20} {'─' * 10} {'─' * 15}")
    if isinstance(data, dict):
        for name, info in data.items():
            ptype = info.get("type", "?") if isinstance(info, dict) else "?"
            configured = info.get("configured", False) if isinstance(info, dict) else False
            status_str = green("configured") if configured else red("not configured")
            print(f"  {cyan(name):<29} {ptype:<10} {status_str}")
    print()


# -- Health --

def cmd_health(args):
    data = _get("/health")
    if _JSON_MODE:
        _print_json(data)
        return
    status = data.get("status", "unknown")
    status_str = green(bold("HEALTHY")) if status == "ok" else red(bold(status.upper()))
    print()
    print(f"  Status: {status_str}")
    print(f"  Uptime: {yellow(str(data.get('uptime_seconds', 0)))}s")
    print(f"  DB:     {green(data.get('db', '?')) if data.get('db') == 'ok' else red(str(data.get('db', '?')))}")
    providers = data.get("providers", {})
    if providers:
        print(f"  Providers:")
        for name, st in providers.items():
            st_str = green(st) if st == "ok" else yellow(st)
            print(f"    {cyan(name)}: {st_str}")
    print()


# -- Stream monitoring --

def cmd_stream(args):
    action = args.action

    if action == "start":
        if not args.url:
            _die("Provide a stream URL: safeeye stream start <url>")
        body = {"url": args.url, "interval": args.interval}
        if args.webhook:
            body["webhook_url"] = args.webhook
        data = _post("/api/v1/stream/start", json_body=body)
        _success(f"Stream monitor started for: {args.url}")
        _print_json(data)

    elif action == "stop":
        if not args.url:
            _die("Provide a stream URL: safeeye stream stop <url>")
        data = _post("/api/v1/stream/stop", json_body={"url": args.url})
        _success(f"Stream monitor stopped for: {args.url}")

    elif action == "status":
        data = _get("/api/v1/stream/status")
        _print_json(data)

    else:
        _die(f"Unknown stream action: {action}. Use: start, stop, status")


# -- Similar --

def cmd_similar(args):
    params = {"phash": args.phash, "threshold": args.threshold}
    data = _get("/api/v1/scan/similar", params=params)
    _print_json(data)


# -- Analytics --

def cmd_analytics(args):
    data = _get("/api/v1/admin/analytics")
    _print_json(data)


# -- Deploy --

def cmd_deploy(args):
    _warn("Triggering remote deploy...")
    data = _post("/api/v1/admin/deploy")
    _success("Deploy triggered.")
    _print_json(data)


# -- Update check --

def cmd_update_check(args):
    data = _get("/api/v1/admin/check-update")
    if _JSON_MODE:
        _print_json(data)
        return
    print()
    print(f"  Local version:  {cyan(str(data.get('local_version', '?')))}")
    print(f"  Remote SHA:     {cyan(str(data.get('remote_sha', '?')))}")
    print(f"  Remote message: {data.get('remote_message', '?')}")
    print(f"  Remote date:    {data.get('remote_date', '?')}")
    update = data.get("update_available", False)
    if update:
        print(f"  {yellow('Update may be available.')}")
        print(f"  Install: {dim(str(data.get('install_command', '')))}")
    else:
        print(f"  {green('Up to date.')}")
    print()


# -- Config --

def cmd_config(args):
    if args.action == "set":
        if not args.key or not args.value:
            _die("Usage: safeeye config set KEY VALUE")
        # Write to ~/.safeeye
        lines = []
        found = False
        if _CONFIG_FILE.exists():
            for line in _CONFIG_FILE.read_text().splitlines():
                stripped = line.strip()
                if stripped and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip().lower()
                    if k == args.key.lower():
                        lines.append(f"{args.key.lower()} = {args.value}")
                        found = True
                        continue
                lines.append(line)
        if not found:
            lines.append(f"{args.key.lower()} = {args.value}")
        _CONFIG_FILE.write_text("\n".join(lines) + "\n")
        _success(f"Config updated: {args.key} = {args.value}")
        return

    # Show current config
    cfg = _load_config()
    if _JSON_MODE:
        # Mask token for safety
        safe = dict(cfg)
        if safe.get("token"):
            safe["token"] = safe["token"][:8] + "..." if len(safe["token"]) > 8 else "***"
        _print_json(safe)
        return

    print()
    print(f"  {bold('SafeEye CLI Configuration')}")
    print()
    print(f"  Config file: {dim(str(_CONFIG_FILE))}")
    print(f"  URL:         {cyan(cfg.get('url') or red('(not set)'))}")
    token = cfg.get("token", "")
    if token:
        masked = token[:8] + "..." if len(token) > 8 else "***"
        print(f"  Token:       {yellow(masked)}")
    else:
        print(f"  Token:       {red('(not set)')}")

    # Show env overrides
    env_url = os.environ.get("SAFEEYE_URL")
    env_tok = os.environ.get("SAFEEYE_TOKEN")
    if env_url or env_tok:
        print()
        print(f"  {dim('Environment overrides:')}")
        if env_url:
            print(f"    SAFEEYE_URL   = {env_url}")
        if env_tok:
            print(f"    SAFEEYE_TOKEN = {env_tok[:8]}...")
    print()


# ──────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safeeye",
        description="SafeEye CLI — Content Safety Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  safeeye scan photo.jpg               Scan a local file\n"
            "  safeeye scan https://i.imgur.com/x.jpg  Scan a URL\n"
            "  safeeye scan --batch url1 url2 url3   Batch scan URLs\n"
            "  safeeye scan --async https://...       Async scan\n"
            "  safeeye tokens list                   List API tokens\n"
            "  safeeye stats                         Overview stats\n"
            "  safeeye health                        Server health\n"
            "  safeeye config set url http://localhost:1985\n"
        ),
    )
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="Machine-readable JSON output")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress informational messages")

    sub = parser.add_subparsers(dest="command", help="Command")

    # ── scan ──
    p_scan = sub.add_parser("scan", help="Scan a file or URL")
    p_scan.add_argument("targets", nargs="*", help="File path(s) or URL(s) to scan")
    p_scan.add_argument("--batch", action="store_true", help="Batch scan multiple URLs")
    p_scan.add_argument("--async", action="store_true", dest="async_mode",
                        help="Submit async scan (returns job_id)")
    p_scan.add_argument("--webhook", type=str, default=None,
                        help="Webhook URL for async/batch results")
    p_scan.add_argument("--no-wait", action="store_true",
                        help="Don't poll for async/batch results")

    # ── job ──
    p_job = sub.add_parser("job", help="Check async job status")
    p_job.add_argument("job_id", help="Job ID to check")

    # ── batch ──
    p_batch = sub.add_parser("batch", help="Check batch status")
    p_batch.add_argument("batch_id", help="Batch ID to check")

    # ── tokens ──
    p_tokens = sub.add_parser("tokens", help="Manage API tokens")
    p_tokens.add_argument("action", choices=["list", "create", "revoke", "rotate"],
                          help="Token action")
    p_tokens.add_argument("name", nargs="?", default=None, help="Token name")
    p_tokens.add_argument("--expires", type=int, default=None,
                          help="Expiry in days (for create)")

    # ── stats ──
    p_stats = sub.add_parser("stats", help="Statistics and history")
    p_stats.add_argument("sub", nargs="?", default=None,
                         choices=["providers", "history", "export"],
                         help="Stats sub-command")
    p_stats.add_argument("--nsfw-only", action="store_true", help="Show only NSFW results")
    p_stats.add_argument("--limit", type=int, default=50, help="History limit")
    p_stats.add_argument("--offset", type=int, default=0, help="History offset")
    p_stats.add_argument("--format", choices=["csv", "json"], default="json",
                         help="Export format")
    p_stats.add_argument("--output", "-o", type=str, default=None,
                         help="Export output file path")

    # ── providers ──
    sub.add_parser("providers", help="List provider status")

    # ── health ──
    sub.add_parser("health", help="Server health check")

    # ── stream ──
    p_stream = sub.add_parser("stream", help="Live stream monitoring")
    p_stream.add_argument("action", choices=["start", "stop", "status"],
                          help="Stream action")
    p_stream.add_argument("url", nargs="?", default=None, help="Stream URL")
    p_stream.add_argument("--interval", type=int, default=10,
                          help="Capture interval in seconds (default: 10)")
    p_stream.add_argument("--webhook", type=str, default=None,
                          help="Webhook URL for NSFW alerts")

    # ── similar ──
    p_similar = sub.add_parser("similar", help="Find similar scans by perceptual hash")
    p_similar.add_argument("phash", help="Perceptual hash (hex string)")
    p_similar.add_argument("--threshold", type=int, default=10,
                           help="Max Hamming distance (0=exact, default 10)")

    # ── analytics ──
    sub.add_parser("analytics", help="Full server analytics")

    # ── deploy ──
    sub.add_parser("deploy", help="Trigger remote auto-deploy")

    # ── update-check ──
    sub.add_parser("update-check", help="Check for SafeEye updates")

    # ── config ──
    p_config = sub.add_parser("config", help="Show or set CLI configuration")
    p_config.add_argument("action", nargs="?", default=None, choices=["set"],
                          help="Config action")
    p_config.add_argument("key", nargs="?", default=None, help="Config key")
    p_config.add_argument("value", nargs="?", default=None, help="Config value")

    return parser


# ──────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────

_DISPATCH = {
    "scan": cmd_scan,
    "job": cmd_job,
    "batch": cmd_batch,
    "tokens": cmd_tokens,
    "stats": cmd_stats,
    "providers": cmd_providers,
    "health": cmd_health,
    "stream": cmd_stream,
    "similar": cmd_similar,
    "analytics": cmd_analytics,
    "deploy": cmd_deploy,
    "update-check": cmd_update_check,
    "config": cmd_config,
}


def main(argv: list[str] | None = None):
    global _QUIET, _JSON_MODE

    parser = build_parser()
    args = parser.parse_args(argv)

    _JSON_MODE = args.json_mode
    _QUIET = args.quiet

    if not args.command:
        parser.print_help()
        sys.exit(0)

    handler = _DISPATCH.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)

    try:
        handler(args)
    except KeyboardInterrupt:
        print()
        _die("Interrupted", 130)
    except requests.exceptions.RequestException as e:
        _die(f"Network error: {e}")


if __name__ == "__main__":
    main()
