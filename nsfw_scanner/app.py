"""FastAPI application for the NSFW Scanner Service."""
import asyncio
import os
import logging
import tempfile
import time as _time_mod
import json as _json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

VERSION = "6.1.0"

from nsfw_scanner import db as database
from nsfw_scanner import auth, stats
from nsfw_scanner.scanner import scan_file, get_active_providers, compute_phash
from nsfw_scanner.vector_store import VectorStore
from nsfw_scanner.gossip import gossip_node
from nsfw_scanner.stream_monitor import (
    start_monitor, stop_monitor, get_all_monitors,
)
from nsfw_scanner.models import (
    ScanResponse, TokenCreate, TokenCreated, TokenInfo,
    FeedbackRequest, StatsOverview, ProviderStats, HistoryItem,
)


# ========== Structured JSON Logging ==========

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return _json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


_json_handler = logging.StreamHandler()
_json_handler.setFormatter(JSONFormatter())
logging.root.handlers.clear()
logging.root.addHandler(_json_handler)
logging.root.setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ========== Module-level start time for uptime ==========
_start_time = _time_mod.monotonic()

# ========== In-memory vector store for pHash similarity ==========
_vector_store = VectorStore()

# ========== Prometheus Metrics (no external deps) ==========
_metrics = {
    "scans_total_nsfw": 0,
    "scans_total_safe": 0,
    "scan_durations": [],  # last 100 durations
    "provider_scans": {},  # provider -> count
    "provider_errors": {},  # provider -> count
}


def _record_scan_metrics(result):
    """Update in-memory Prometheus metrics after a scan."""
    if result.is_nsfw:
        _metrics["scans_total_nsfw"] += 1
    else:
        _metrics["scans_total_safe"] += 1

    # Track duration (keep last 100)
    _metrics["scan_durations"].append(result.scan_duration_ms / 1000.0)
    if len(_metrics["scan_durations"]) > 100:
        _metrics["scan_durations"] = _metrics["scan_durations"][-100:]

    # Per-provider metrics
    for pr in result.provider_results:
        _metrics["provider_scans"][pr.provider] = _metrics["provider_scans"].get(pr.provider, 0) + 1
        if pr.error:
            _metrics["provider_errors"][pr.provider] = _metrics["provider_errors"].get(pr.provider, 0) + 1

# ========== File size limit ==========
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024

# Simple in-memory rate limiter
_rate_limits: dict = {}  # token_hash -> (count, window_start)
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))

def check_rate_limit(token_name: str):
    import time
    now = time.time()
    key = token_name or "anon"
    count, start = _rate_limits.get(key, (0, now))
    if now - start > 60:
        _rate_limits[key] = (1, now)
        return
    if count >= RATE_LIMIT_PER_MINUTE:
        from fastapi import HTTPException
        raise HTTPException(429, f"Rate limit exceeded ({RATE_LIMIT_PER_MINUTE}/min). Try again later.")
    _rate_limits[key] = (count + 1, start)

TEMP_DIR = os.environ.get("SCAN_TEMP_DIR", "/tmp/nsfw_scans")


def _check_file_size(content: bytes):
    """Raise 413 if content exceeds MAX_FILE_SIZE."""
    if len(content) > MAX_FILE_SIZE:
        max_mb = MAX_FILE_SIZE // (1024 * 1024)
        raise HTTPException(413, f"File too large. Maximum: {max_mb}MB")


async def _check_phash_cache(file_path: str) -> dict | None:
    """Compute pHash and check DB for an exact match (hamming distance 0).
    Returns cached scan result dict if found, else None."""
    phash = compute_phash(file_path)
    if not phash:
        return None
    similar = await database.find_similar_by_phash(phash, threshold=0, limit=1)
    if similar:
        logger.info(f"Cache hit for pHash {phash}")
        cached = similar[0]
        return cached
    return None


async def _send_telegram_alert(labels: list, confidence: float):
    """Send a Telegram alert to admin if bot credentials are configured in DB."""
    try:
        saved = await database.load_all_provider_config()
        bot_token = saved.get("TELEGRAM_BOT_TOKEN")
        chat_id = saved.get("TELEGRAM_ADMIN_CHAT_ID")
        if not bot_token or not chat_id:
            return
        import aiohttp
        text = f"\U0001f6a8 NSFW detected! Labels: {', '.join(labels)} | Confidence: {round(confidence * 100)}%"
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


def _is_master(token_data: dict) -> bool:
    """Check if the token is the master token."""
    return token_data.get("name") == "_master"


async def _send_email_alert(subject: str, body: str):
    """Send an email alert in the background using SMTP. Non-blocking via asyncio.to_thread."""
    smtp_host = os.environ.get("EMAIL_SMTP_HOST")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    smtp_user = os.environ.get("EMAIL_SMTP_USER")
    smtp_pass = os.environ.get("EMAIL_SMTP_PASS")
    alert_to = os.environ.get("EMAIL_ALERT_TO")

    if not all([smtp_host, smtp_user, smtp_pass, alert_to]):
        return  # Email not configured, silently skip

    def _send():
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = alert_to
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            logger.info(f"Email alert sent to {alert_to}: {subject}")
        except Exception as e:
            logger.warning(f"Email alert failed: {e}")

    try:
        await asyncio.to_thread(_send)
    except Exception as e:
        logger.warning(f"Email alert thread failed: {e}")


import re as _re

def _extract_og_image(html: str) -> str | None:
    """Extract og:image URL from HTML."""
    for pattern in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]:
        m = _re.search(pattern, html, _re.IGNORECASE)
        if m:
            return m.group(1)
    return None
async def _update_blocklist():
    """Download a remote blocklist and merge new domains into nsfw_domains.txt."""
    url = os.environ.get("NSFW_BLOCKLIST_UPDATE_URL", "")
    if not url:
        return
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning(f"Blocklist update failed: HTTP {resp.status}")
                    return
                text = await resp.text()

        # Parse domains (one per line, skip comments/blanks)
        new_domains = set()
        for line in text.splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                # Handle hosts-file format: "0.0.0.0 domain.com"
                parts = line.split()
                domain = parts[-1] if parts else ""
                if "." in domain and len(domain) > 3:
                    new_domains.add(domain)

        if not new_domains:
            logger.info("Blocklist update: no valid domains found in response")
            return

        # Merge into nsfw_domains.txt
        blocklist_paths = [
            "/app/services/nsfw_domains.txt",
            os.path.join(os.path.dirname(__file__), "..", "services", "nsfw_domains.txt"),
        ]
        target = None
        existing = set()
        for p in blocklist_paths:
            if os.path.isfile(p):
                target = p
                with open(p, "r") as f:
                    for line in f:
                        line = line.strip().lower()
                        if line and not line.startswith("#"):
                            existing.add(line)
                break

        if target is None:
            # Create in first writable location
            target = blocklist_paths[0]
            os.makedirs(os.path.dirname(target), exist_ok=True)

        added = new_domains - existing
        if added:
            with open(target, "a") as f:
                f.write(f"\n# Auto-updated from remote blocklist ({datetime.utcnow().isoformat()})\n")
                for domain in sorted(added):
                    f.write(domain + "\n")
            logger.info(f"Blocklist update: added {len(added)} new domains (total: {len(existing) + len(added)})")
        else:
            logger.info("Blocklist update: no new domains to add")
    except Exception as e:
        logger.error(f"Blocklist update error: {e}")


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_PORT = 1985


def find_available_port(preferred: int = DEFAULT_PORT) -> int:
    """Find an available port, starting from preferred."""
    import socket
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available port found near {preferred}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, preload NudeNet model."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    # Add pip_extras to path (packages installed via dashboard)
    _extras = "/app/pip_extras"
    if os.path.isdir(_extras):
        import sys as _s, site
        if _extras not in _s.path:
            _s.path.insert(0, _extras)
            site.addsitedir(_extras)
    await database.init_db()
    logger.info("Database initialized")

    # Restore saved provider credentials from DB
    saved_config = await database.load_all_provider_config()
    for key, value in saved_config.items():
        os.environ[key] = value
    if saved_config:
        logger.info(f"Restored {len(saved_config)} provider config keys from DB")

    # Restore disabled providers
    disabled_str = saved_config.get("DISABLED_PROVIDERS", "")
    if disabled_str:
        from nsfw_scanner.scanner import load_disabled_providers
        load_disabled_providers(set(disabled_str.split(",")))

    # Preload models to avoid cold-start on first request
    try:
        from nsfw_scanner.providers.nudenet_provider import _get_detector
        _get_detector()
        logger.info("NudeNet model preloaded")
    except Exception as e:
        logger.warning(f"NudeNet preload failed: {e}")

    # Log active providers (don't let a broken provider crash startup)
    try:
        active = get_active_providers()
        logger.info(f"Active providers: {active}")
    except Exception as e:
        logger.warning(f"Provider check failed: {e}")

    # Load existing pHashes into in-memory vector store
    try:
        all_phashes = await database.get_all_phashes()
        _vector_store.load_from_db(all_phashes)
        logger.info(f"Vector store loaded with {len(_vector_store)} pHash entries")
    except Exception as e:
        logger.warning(f"Vector store preload failed: {e}")

    # Start P2P Network (zero-config)
    gossip_enabled = saved_config.get("GOSSIP_ENABLED", "0") == "1"
    server_id = saved_config.get("GOSSIP_SERVER_ID", "")
    server_key = saved_config.get("GOSSIP_SERVER_KEY", "")
    if gossip_enabled:
        gossip_node.configure(True, server_id, server_key)
        gossip_node.on_hash(lambda rec: database.import_hash_metadata([rec], "gossip"))
        # Save generated ID/key if new
        if gossip_node.server_id != server_id:
            await database.save_provider_config("GOSSIP_SERVER_ID", gossip_node.server_id)
        if gossip_node.server_key != server_key:
            await database.save_provider_config("GOSSIP_SERVER_KEY", gossip_node.server_key)
        await gossip_node.start()
        logger.info(f"P2P Network started — server_id={gossip_node.server_id}")

    # Start periodic cleanup task
    import asyncio
    async def _cleanup_loop():
        _blocklist_counter = 0  # counts hours since last blocklist update
        while True:
            await asyncio.sleep(3600)  # Every hour
            try:
                import glob, time
                count = 0
                for f in glob.glob(os.path.join(TEMP_DIR, "*")):
                    if time.time() - os.path.getmtime(f) > 3600:
                        os.unlink(f)
                        count += 1
                if count:
                    logger.info(f"Cleanup: removed {count} stale temp files")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

            # Blocklist auto-update (every 24h)
            _blocklist_counter += 1
            if _blocklist_counter >= 24:
                _blocklist_counter = 0
                if os.environ.get("NSFW_BLOCKLIST_AUTO_UPDATE", "").lower() == "true":
                    try:
                        await _update_blocklist()
                    except Exception as e:
                        logger.error(f"Scheduled blocklist update failed: {e}")
    asyncio.create_task(_cleanup_loop())

    # Start webhook retry queue processor (every 30 seconds, exponential backoff)
    async def _webhook_retry_loop():
        import aiohttp as _wh_aio
        while True:
            await asyncio.sleep(30)
            try:
                pending = await database.get_pending_webhooks()
                for wh in pending:
                    wh_id = wh["id"]
                    attempts = wh["attempts"]
                    max_attempts = wh["max_attempts"]
                    try:
                        async with _wh_aio.ClientSession() as session:
                            async with session.post(
                                wh["webhook_url"],
                                data=wh["payload"],
                                headers={"Content-Type": "application/json"},
                                timeout=_wh_aio.ClientTimeout(total=10),
                            ) as resp:
                                if resp.status < 400:
                                    await database.update_webhook_status(wh_id, "delivered", attempts=attempts + 1)
                                    logger.info(f"Webhook {wh_id} delivered (attempt {attempts + 1})")
                                else:
                                    raise Exception(f"HTTP {resp.status}")
                    except Exception as e:
                        new_attempts = attempts + 1
                        if new_attempts >= max_attempts:
                            await database.update_webhook_status(wh_id, "failed", attempts=new_attempts)
                            logger.error(f"Webhook {wh_id} permanently failed after {new_attempts} attempts: {e}")
                        else:
                            # Exponential backoff: 30s, 60s, 120s, 240s, 480s
                            backoff_seconds = 30 * (2 ** attempts)
                            next_retry = (datetime.utcnow() + timedelta(seconds=backoff_seconds)).isoformat()
                            await database.update_webhook_status(wh_id, "pending", attempts=new_attempts, next_retry=next_retry)
                            logger.warning(f"Webhook {wh_id} attempt {new_attempts} failed, retry in {backoff_seconds}s: {e}")
            except Exception as e:
                logger.error(f"Webhook retry loop error: {e}")
    asyncio.create_task(_webhook_retry_loop())

    yield

    # Cleanup temp
    import shutil
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


app = FastAPI(
    title="SafeEyes",
    description="AI-powered content safety scanner with multi-provider parallel detection",
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ========== CORS ==========
# When behind Nginx that already sets Access-Control-Allow-Origin,
# skip the middleware to avoid duplicate headers (browsers reject double origins).
if not os.environ.get("BEHIND_NGINX"):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ========== Concurrent Scan Limit ==========
MAX_CONCURRENT_SCANS = int(os.environ.get("MAX_CONCURRENT_SCANS", "10"))
_scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)


# ========== Auth Dependencies ==========

async def require_token(authorization: str = Header(None)) -> dict:
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    raw = authorization.removeprefix("Bearer ").strip()
    # Master token has full access to all endpoints
    if auth.verify_master(raw):
        return {"name": "_master", "token_hash": "master", "priority": 0}
    token_data = await auth.verify_api_token(raw)
    if not token_data:
        raise HTTPException(401, "Invalid or expired token")
    await database.bump_token_usage(auth.hash_token(raw))
    return token_data


async def require_master(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    raw = authorization.removeprefix("Bearer ").strip()
    if not auth.verify_master(raw):
        # Fall back to regular token check — master can also be an API token
        raise HTTPException(403, "Master token required")


# ========== Scan Endpoints ==========

@app.post("/api/v1/scan/file", response_model=ScanResponse)
async def scan_file_endpoint(
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    token_data = await require_token(authorization)
    check_rate_limit(token_data.get("name"))

    # Save upload to temp
    suffix = os.path.splitext(file.filename or "upload")[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=suffix, delete=False)
    try:
        content = await file.read()
        _check_file_size(content)
        tmp.write(content)
        tmp.close()

        # pHash cache check
        cached = await _check_phash_cache(tmp.name)
        if cached:
            return ScanResponse(
                scan_id=cached["id"],
                result=cached,
                timestamp=datetime.utcnow().isoformat(),
            )

        # Priority 0 tokens (master) skip the semaphore queue
        if token_data.get("priority", 1) == 0 or _is_master(token_data):
            result = await scan_file(tmp.name)
        else:
            if _scan_semaphore.locked():
                logger.info("Scan semaphore full, waiting for a slot...")
            async with _scan_semaphore:
                result = await scan_file(tmp.name)

        # Update Prometheus metrics
        _record_scan_metrics(result)

        # Persist to DB
        await database.insert_scan(
            result.scan_id,
            "image" if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else "video",
            result.model_dump(),
            token_data.get("name"),
        )

        # Add to in-memory vector store + gossip broadcast
        if result.phash:
            _vector_store.add(result.scan_id, result.phash)
            asyncio.create_task(gossip_node.broadcast_hash({
                "p": result.phash, "n": int(result.is_nsfw),
                "c": round(result.confidence, 2), "l": result.labels,
            }))

        # Alerts for NSFW
        if result.is_nsfw:
            await _send_telegram_alert(result.labels, result.confidence)
            asyncio.create_task(_send_email_alert(
                "SafeEyes NSFW Alert",
                f"NSFW detected!\nLabels: {', '.join(result.labels)}\nConfidence: {round(result.confidence * 100)}%\nScan ID: {result.scan_id}",
            ))

        return ScanResponse(
            scan_id=result.scan_id,
            result=result,
            timestamp=datetime.utcnow().isoformat(),
        )
    finally:
        os.unlink(tmp.name)


@app.post("/api/v1/scan/url", response_model=ScanResponse)
async def scan_url_endpoint(
    url: str = Query(...),
    authorization: str = Header(None),
):
    token_data = await require_token(authorization)
    check_rate_limit(token_data.get("name"))

    # Download the URL to temp
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise HTTPException(400, f"Failed to download URL: HTTP {resp.status}")
                content = await resp.read()
                content_type = resp.headers.get("Content-Type", "")
    except aiohttp.ClientError as e:
        raise HTTPException(400, f"Download error: {e}")

    _check_file_size(content)

    if content_type and not any(t in content_type for t in ["image", "video", "octet-stream"]):
        # Try og:image extraction from HTML
        og_url = _extract_og_image(content.decode("utf-8", errors="ignore")) if "html" in content_type else None
        if og_url:
            import aiohttp as _aio2
            try:
                async with _aio2.ClientSession() as s2:
                    async with s2.get(og_url, timeout=_aio2.ClientTimeout(total=15)) as r2:
                        if r2.status == 200:
                            content = await r2.read()
                            content_type = r2.headers.get("Content-Type", "image/jpeg")
                            logger.info(f"Extracted og:image: {og_url[:80]}")
                        else:
                            raise HTTPException(400, f"og:image download failed: HTTP {r2.status}")
            except _aio2.ClientError as e:
                raise HTTPException(400, f"og:image download failed: {e}")
        else:
            raise HTTPException(400,
                f"URL is not an image or video (Content-Type: {content_type}). "
                "No og:image found. Provide a direct link to a media file.")

    ext = ".jpg"
    if "png" in content_type:
        ext = ".png"
    elif "webp" in content_type:
        ext = ".webp"
    elif "video" in content_type or "mp4" in content_type:
        ext = ".mp4"

    tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=ext, delete=False)
    try:
        tmp.write(content)
        tmp.close()

        # pHash cache check
        cached = await _check_phash_cache(tmp.name)
        if cached:
            return ScanResponse(
                scan_id=cached["id"],
                result=cached,
                timestamp=datetime.utcnow().isoformat(),
            )

        # Priority 0 tokens (master) skip the semaphore queue
        if token_data.get("priority", 1) == 0 or _is_master(token_data):
            result = await scan_file(tmp.name)
        else:
            if _scan_semaphore.locked():
                logger.info("Scan semaphore full, waiting for a slot...")
            async with _scan_semaphore:
                result = await scan_file(tmp.name)

        # Update Prometheus metrics
        _record_scan_metrics(result)

        await database.insert_scan(
            result.scan_id,
            "image" if ext in {".jpg", ".png", ".webp"} else "video",
            result.model_dump(),
            token_data.get("name"),
        )

        # Add to in-memory vector store + gossip broadcast
        if result.phash:
            _vector_store.add(result.scan_id, result.phash)
            asyncio.create_task(gossip_node.broadcast_hash({
                "p": result.phash, "n": int(result.is_nsfw),
                "c": round(result.confidence, 2), "l": result.labels,
            }))

        # Alerts for NSFW
        if result.is_nsfw:
            await _send_telegram_alert(result.labels, result.confidence)
            asyncio.create_task(_send_email_alert(
                "SafeEyes NSFW Alert",
                f"NSFW detected!\nLabels: {', '.join(result.labels)}\nConfidence: {round(result.confidence * 100)}%\nScan ID: {result.scan_id}",
            ))

        return ScanResponse(
            scan_id=result.scan_id,
            result=result,
            timestamp=datetime.utcnow().isoformat(),
        )
    finally:
        os.unlink(tmp.name)


@app.get("/api/v1/scan/similar")
async def find_similar_scans(
    phash: str = Query(..., description="Perceptual hash to search for"),
    threshold: int = Query(10, ge=0, le=64, description="Max Hamming distance (0=exact, 10=default)"),
    authorization: str = Header(None),
):
    """Find scans with similar perceptual hashes (hamming distance < threshold)."""
    await require_token(authorization)
    if not phash or not all(c in "0123456789abcdef" for c in phash.lower()):
        raise HTTPException(400, "Invalid phash: must be a hex string")
    results = await database.find_similar_by_phash(phash.lower(), threshold=threshold)
    return {"phash": phash, "threshold": threshold, "matches": len(results), "results": results}


@app.get("/api/v1/scan/vector-search")
async def vector_search(
    phash: str = Query(..., description="Perceptual hash (hex) to search for"),
    top_k: int = Query(10, ge=1, le=100, description="Number of most similar results to return"),
    authorization: str = Header(None),
):
    """Fast in-memory vector similarity search over pHash fingerprints."""
    await require_token(authorization)
    if not phash or not all(c in "0123456789abcdef" for c in phash.lower()):
        raise HTTPException(400, "Invalid phash: must be a hex string")
    results = _vector_store.search(phash.lower(), top_k=top_k)
    return {
        "phash": phash,
        "top_k": top_k,
        "total_indexed": len(_vector_store),
        "results": [{"scan_id": sid, "similarity": round(sim, 4)} for sid, sim in results],
    }


# ========== Text-to-Image Search ==========


class _SearchRequest(BaseModel):
    query: str
    limit: int = 50


@app.post("/api/v1/scan/search")
async def search_scans_by_text(
    body: _SearchRequest,
    authorization: str = Header(None),
):
    """Search past scans by keyword matching against stored detection labels.

    Accepts a text query (e.g. "nudity", "weapons", "gore") and returns scans
    whose provider-assigned labels contain all query tokens.
    """
    await require_token(authorization)
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Query must not be empty")
    limit = max(1, min(body.limit, 200))
    results = await database.search_scans_by_labels(query, limit=limit)
    return {"query": query, "matches": len(results), "results": results}


# ========== Delta Detection ==========

def _hamming_distance(h1: str, h2: str) -> int:
    """Compute the Hamming distance between two hex hash strings."""
    if len(h1) != len(h2):
        # Pad the shorter one with zeros
        max_len = max(len(h1), len(h2))
        h1 = h1.zfill(max_len)
        h2 = h2.zfill(max_len)
    val = int(h1, 16) ^ int(h2, 16)
    return bin(val).count("1")


def _compare_images(path1: str, path2: str) -> dict:
    """Compare two images using dimensions, file size, and color histograms."""
    from PIL import Image as _PILImage

    img1 = _PILImage.open(path1)
    img2 = _PILImage.open(path2)

    changes = []
    if img1.size != img2.size:
        changes.append(f"resolution: {img1.size} vs {img2.size}")

    size1 = os.path.getsize(path1)
    size2 = os.path.getsize(path2)
    if size1 != size2:
        changes.append(f"file_size: {size1} vs {size2}")

    # Compare color histograms
    h1 = img1.convert("RGB").histogram()
    h2 = img2.convert("RGB").histogram()
    dot = sum(a * b for a, b in zip(h1, h2))
    mag1 = sum(a * a for a in h1) ** 0.5
    mag2 = sum(b * b for b in h2) ** 0.5
    correlation = dot / (mag1 * mag2 + 1e-10)

    # Pixel diff
    pixel_diff_pct = 0.0
    diff_image_b64 = None
    try:
        import numpy as np, base64, io
        common_size = (min(img1.width, img2.width), min(img1.height, img2.height))
        a = np.array(img1.convert("RGB").resize(common_size), dtype=np.float32)
        b = np.array(img2.convert("RGB").resize(common_size), dtype=np.float32)
        diff = np.abs(a - b)
        pixel_diff_pct = round((diff > 25).mean() * 100, 2)
        diff_mask = (diff.mean(axis=2) > 25).astype(np.uint8) * 255
        diff_img = _PILImage.fromarray(diff_mask, mode='L')
        buf = io.BytesIO()
        diff_img.save(buf, format='PNG')
        diff_image_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass

    return {
        "image1_size": list(img1.size),
        "image2_size": list(img2.size),
        "file_size1": size1,
        "file_size2": size2,
        "histogram_correlation": round(correlation, 4),
        "pixel_diff_pct": pixel_diff_pct,
        "diff_image_base64": diff_image_b64,
        "changes": changes,
    }


async def _download_to_tmp(url: str, label: str) -> str:
    """Download a URL to a temp file and return its path."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise HTTPException(400, f"Failed to download {label}: HTTP {resp.status}")
                content = await resp.read()
                content_type = resp.headers.get("Content-Type", "")
    except aiohttp.ClientError as e:
        raise HTTPException(400, f"Download error for {label}: {e}")

    _check_file_size(content)

    ext = ".jpg"
    if "png" in content_type:
        ext = ".png"
    elif "webp" in content_type:
        ext = ".webp"
    elif "video" in content_type or "mp4" in content_type:
        ext = ".mp4"

    tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=ext, delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


@app.post("/api/v1/scan/compare")
async def compare_endpoint(
    url1: str = Query(None),
    url2: str = Query(None),
    file1: UploadFile = File(None),
    file2: UploadFile = File(None),
    authorization: str = Header(None),
):
    """Compare two images/videos and return delta detection results.

    Accepts either query params (url1, url2) or multipart files (file1, file2).
    Computes pHash for both, calculates hamming distance, and for images compares
    dimensions, file size, and color histograms.
    """
    token_data = await require_token(authorization)
    check_rate_limit(token_data.get("name"))

    tmp_paths = []
    try:
        # Resolve inputs to temp files
        if file1 and file2:
            content1 = await file1.read()
            content2 = await file2.read()
            _check_file_size(content1)
            _check_file_size(content2)

            t1 = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=".tmp", delete=False)
            t1.write(content1)
            t1.close()
            tmp_paths.append(t1.name)

            t2 = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=".tmp", delete=False)
            t2.write(content2)
            t2.close()
            tmp_paths.append(t2.name)
        elif url1 and url2:
            p1 = await _download_to_tmp(url1, "url1")
            tmp_paths.append(p1)
            p2 = await _download_to_tmp(url2, "url2")
            tmp_paths.append(p2)
        else:
            raise HTTPException(400, "Provide either (url1, url2) or (file1, file2)")

        path1, path2 = tmp_paths[0], tmp_paths[1]

        # Compute perceptual hashes
        phash1 = compute_phash(path1)
        phash2 = compute_phash(path2)

        result = {
            "phash1": phash1,
            "phash2": phash2,
        }

        changes = []

        if phash1 and phash2:
            hd = _hamming_distance(phash1, phash2)
            # Similarity: 1.0 means identical, 0.0 means completely different
            max_bits = max(len(phash1), len(phash2)) * 4  # each hex char = 4 bits
            similarity = round(1.0 - (hd / max(max_bits, 1)), 4)
            result["hamming_distance"] = hd
            result["similarity"] = similarity
            if hd == 0:
                changes.append("hash_identical")
            elif hd <= 10:
                changes.append("hash_similar")
            else:
                changes.append("hash_different")
        else:
            result["hamming_distance"] = None
            result["similarity"] = None
            changes.append("phash_unavailable")

        # Compare file sizes
        size1 = os.path.getsize(path1)
        size2 = os.path.getsize(path2)
        if size1 != size2:
            changes.append("size_different")

        # Try image-level comparison
        try:
            image_details = _compare_images(path1, path2)
            result["image_comparison"] = image_details
            changes.extend(image_details.get("changes", []))
        except Exception:
            result["image_comparison"] = None

        result["changes"] = changes
        return result

    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                os.unlink(p)


@app.get("/api/v1/scan/{scan_id}")
async def get_scan_result(scan_id: str, authorization: str = Header(None)):
    token_data = await require_token(authorization)
    # Multi-tenant isolation: non-master tokens only see their own scans
    requesting_token = None if _is_master(token_data) else token_data.get("name")
    scan = await database.get_scan(scan_id, requesting_token=requesting_token)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return scan


# ========== Async Scan + Webhooks + Batch ==========

async def _process_job(job_id: str, file_path: str, webhook_url: str = None):
    """Background task: run scan, update job, queue webhook for persistent delivery."""
    try:
        await database.update_job(job_id, "processing")
        result = await scan_file(file_path)
        result_json = json.dumps(result.model_dump())

        await database.update_job(job_id, "completed", result_json=result_json)
        await database.insert_scan(result.scan_id, "image", result.model_dump())

        # Add to in-memory vector store
        if result.phash:
            _vector_store.add(result.scan_id, result.phash)

        # Queue webhook for persistent retry delivery instead of direct call
        if webhook_url:
            payload = _json.dumps({"job_id": job_id, "result": result.model_dump()})
            await database.queue_webhook(job_id, webhook_url, payload)
            logger.info(f"Webhook queued for job {job_id} -> {webhook_url}")

    except Exception as e:
        await database.update_job(job_id, "failed", error=str(e))
        logger.error(f"Job {job_id} failed: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.unlink(file_path)


import json


@app.post("/api/v1/scan/async")
async def scan_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    url: str = Query(None),
    webhook_url: str = Query(None),
    authorization: str = Header(None),
):
    """Submit scan asynchronously. Returns job_id immediately."""
    token_data = await require_token(authorization)

    if not file and not url:
        raise HTTPException(400, "Provide file or url")

    job_id = _uuid.uuid4().hex[:16]

    if url:
        # Download first, then process in background
        import aiohttp as _aio
        try:
            async with _aio.ClientSession() as session:
                async with session.get(url, timeout=_aio.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise HTTPException(400, f"HTTP {resp.status}")
                    ct = resp.headers.get("Content-Type", "")
                    if ct and not any(t in ct for t in ["image", "video", "octet-stream"]):
                        raise HTTPException(400, f"Not an image/video: {ct}")
                    content = await resp.read()
        except _aio.ClientError as e:
            raise HTTPException(400, str(e))

        ext = ".jpg"
        if "png" in ct: ext = ".png"
        elif "video" in ct: ext = ".mp4"
        tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=ext, delete=False)
        tmp.write(content)
        tmp.close()
        file_path = tmp.name
    else:
        suffix = os.path.splitext(file.filename or "upload")[1] or ".jpg"
        tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=suffix, delete=False)
        tmp.write(await file.read())
        tmp.close()
        file_path = tmp.name

    await database.create_job(job_id, "url" if url else "file", input_url=url, file_path=file_path,
                              webhook_url=webhook_url, token_name=token_data.get("name"))
    background_tasks.add_task(_process_job, job_id, file_path, webhook_url)

    return {"job_id": job_id, "status": "pending"}


@app.post("/api/v1/scan/batch")
async def scan_batch(
    body: dict,
    background_tasks: BackgroundTasks,
    authorization: str = Header(None),
):
    """Submit multiple URLs for scanning. Returns batch_id."""
    token_data = await require_token(authorization)

    urls = body.get("urls", [])
    if not urls or len(urls) > 100:
        raise HTTPException(400, "Provide 1-100 URLs")

    webhook_url = body.get("webhook_url")
    batch_id = f"batch_{_uuid.uuid4().hex[:12]}"
    import aiohttp as _aio

    for url in urls:
        job_id = _uuid.uuid4().hex[:16]
        # Download each URL
        try:
            async with _aio.ClientSession() as session:
                async with session.get(url, timeout=_aio.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        await database.create_job(job_id, "url", input_url=url, batch_id=batch_id, token_name=token_data.get("name"))
                        await database.update_job(job_id, "failed", error=f"HTTP {resp.status}")
                        continue
                    ct = resp.headers.get("Content-Type", "")
                    if ct and not any(t in ct for t in ["image", "video", "octet-stream"]):
                        await database.create_job(job_id, "url", input_url=url, batch_id=batch_id, token_name=token_data.get("name"))
                        await database.update_job(job_id, "failed", error=f"Not media: {ct}")
                        continue
                    content = await resp.read()
        except Exception as e:
            await database.create_job(job_id, "url", input_url=url, batch_id=batch_id, token_name=token_data.get("name"))
            await database.update_job(job_id, "failed", error=str(e))
            continue

        ext = ".jpg"
        if "png" in ct: ext = ".png"
        elif "video" in ct: ext = ".mp4"
        tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=ext, delete=False)
        tmp.write(content)
        tmp.close()

        await database.create_job(job_id, "url", input_url=url, file_path=tmp.name, webhook_url=webhook_url,
                                  batch_id=batch_id, token_name=token_data.get("name"))
        background_tasks.add_task(_process_job, job_id, tmp.name, webhook_url)

    return {"batch_id": batch_id, "total": len(urls), "status": "processing"}


@app.get("/api/v1/job/{job_id}")
async def get_job_status(job_id: str, authorization: str = Header(None)):
    """Poll job status and result."""
    await require_token(authorization)
    job = await database.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    resp = {"job_id": job["id"], "status": job["status"], "created_at": job["created_at"]}
    if job["completed_at"]:
        resp["completed_at"] = job["completed_at"]
    if job["result"]:
        resp["result"] = json.loads(job["result"])
    if job["error"]:
        resp["error"] = job["error"]
    return resp


@app.get("/api/v1/batch/{batch_id}")
async def get_batch_status(batch_id: str, authorization: str = Header(None)):
    """Get batch progress and results."""
    await require_token(authorization)
    jobs = await database.get_batch_jobs(batch_id)
    if not jobs:
        raise HTTPException(404, "Batch not found")

    completed = [j for j in jobs if j["status"] == "completed"]
    failed = [j for j in jobs if j["status"] == "failed"]
    pending = [j for j in jobs if j["status"] in ("pending", "processing")]

    results = []
    for j in completed:
        r = {"job_id": j["id"], "status": "completed", "url": j.get("input_url")}
        if j["result"]:
            r["result"] = json.loads(j["result"])
        results.append(r)
    for j in failed:
        results.append({"job_id": j["id"], "status": "failed", "url": j.get("input_url"), "error": j.get("error")})

    return {
        "batch_id": batch_id,
        "total": len(jobs),
        "completed": len(completed),
        "failed": len(failed),
        "pending": len(pending),
        "results": results,
    }


# ========== Stream Monitoring ==========

@app.post("/api/v1/stream/start")
async def stream_start(
    body: dict,
    authorization: str = Header(None),
):
    """Start monitoring an RTMP/HLS live stream for NSFW content."""
    await require_master(authorization)

    url = body.get("url")
    if not url:
        raise HTTPException(400, "Missing required field: url")

    interval = body.get("interval", 10)
    if not isinstance(interval, (int, float)) or interval < 1:
        raise HTTPException(400, "interval must be a number >= 1")

    webhook_url = body.get("webhook_url")

    try:
        state = start_monitor(
            stream_url=url,
            interval_seconds=int(interval),
            webhook_url=webhook_url,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    return {
        "status": "started",
        "monitor": state.to_dict(),
    }


@app.post("/api/v1/stream/stop")
async def stream_stop(
    body: dict,
    authorization: str = Header(None),
):
    """Stop monitoring a live stream."""
    await require_master(authorization)

    url = body.get("url")
    if not url:
        raise HTTPException(400, "Missing required field: url")

    stopped = stop_monitor(url)
    if not stopped:
        raise HTTPException(404, f"No active monitor for: {url}")

    return {"status": "stopped", "stream_url": url}


@app.get("/api/v1/stream/status")
async def stream_status(authorization: str = Header(None)):
    """List all active stream monitors and their stats."""
    await require_token(authorization)
    monitors = get_all_monitors()
    return {
        "active_monitors": len(monitors),
        "monitors": monitors,
    }


# ========== Feedback ==========

@app.post("/api/v1/feedback/{scan_id}")
async def submit_feedback(
    scan_id: str,
    body: FeedbackRequest,
    authorization: str = Header(None),
):
    await require_token(authorization)
    scan = await database.get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    await database.insert_feedback(scan_id, body.actual_nsfw, body.notes)
    return {"status": "ok"}


# ========== Stats ==========

@app.get("/api/v1/stats", response_model=StatsOverview)
async def get_stats(authorization: str = Header(None)):
    token_data = await require_token(authorization)
    # Multi-tenant: non-master tokens only see their own stats
    requesting_token = None if _is_master(token_data) else token_data.get("name")
    return await stats.get_overview(requesting_token=requesting_token)


@app.get("/api/v1/stats/providers", response_model=list[ProviderStats])
async def get_provider_stats(authorization: str = Header(None)):
    await require_token(authorization)
    return await stats.get_provider_stats()


@app.get("/api/v1/stats/tokens/{token_name}/usage")
async def get_token_usage(token_name: str, authorization: str = Header(None)):
    """Detailed usage stats for a single API token. Master token required."""
    await require_master(authorization)
    return await stats.get_token_usage(token_name)


@app.get("/api/v1/stats/timeline")
async def get_scan_timeline(days: int = Query(30, le=90), authorization: str = Header(None)):
    """Hourly scan counts for D3 visualizations."""
    await require_token(authorization)
    return await stats.get_scan_timeline(days)


@app.get("/api/v1/stats/providers/{provider_name}/usage")
async def get_provider_usage(provider_name: str, authorization: str = Header(None)):
    """Detailed usage stats for a single provider — calls, latency, daily breakdown, errors."""
    await require_token(authorization)
    return await stats.get_provider_usage(provider_name)


@app.get("/api/v1/stats/history", response_model=list[HistoryItem])
async def get_history(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    nsfw_only: bool = Query(False),
    authorization: str = Header(None),
):
    token_data = await require_token(authorization)
    # Multi-tenant: non-master tokens only see their own history
    requesting_token = None if _is_master(token_data) else token_data.get("name")
    return await stats.get_history(limit, offset, nsfw_only, requesting_token=requesting_token)


@app.get("/api/v1/stats/export")
async def export_scan_history(
    format: str = Query("json", pattern="^(csv|json)$"),
    authorization: str = Header(None),
):
    """Export all scan history as CSV or JSON."""
    token_data = await require_token(authorization)

    db = await database.get_db()
    try:
        # Multi-tenant: non-master tokens only see their own scans
        if _is_master(token_data):
            rows = await db.execute_fetchall(
                "SELECT id, timestamp, is_nsfw, confidence, labels, providers_agree, providers_total, total_duration_ms "
                "FROM scan_history ORDER BY timestamp DESC"
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT id, timestamp, is_nsfw, confidence, labels, providers_agree, providers_total, total_duration_ms "
                "FROM scan_history WHERE requesting_token=? ORDER BY timestamp DESC",
                (token_data.get("name"),),
            )
        scans = [dict(r) for r in rows]
    finally:
        await db.close()

    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["scan_id", "timestamp", "is_nsfw", "confidence", "labels", "providers_agree", "providers_total", "duration_ms"])
        for s in scans:
            writer.writerow([
                s["id"],
                s["timestamp"],
                bool(s["is_nsfw"]),
                s["confidence"],
                s["labels"],
                s["providers_agree"],
                s["providers_total"],
                s["total_duration_ms"],
            ])
        from fastapi.responses import Response
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=scan_history.csv"},
        )
    else:
        # JSON format
        result = []
        for s in scans:
            result.append({
                "scan_id": s["id"],
                "timestamp": s["timestamp"],
                "is_nsfw": bool(s["is_nsfw"]),
                "confidence": s["confidence"],
                "labels": _json.loads(s["labels"]) if isinstance(s["labels"], str) else s["labels"],
                "providers_agree": s["providers_agree"],
                "providers_total": s["providers_total"],
                "duration_ms": s["total_duration_ms"],
            })
        return result


# ========== Token Management ==========

@app.post("/api/v1/admin/tokens", response_model=TokenCreated)
async def create_token(body: TokenCreate, authorization: str = Header(None)):
    await require_master(authorization)
    raw, hashed = auth.generate_token()
    expires_at = None
    if body.expires_in_days:
        expires_at = (datetime.utcnow() + timedelta(days=body.expires_in_days)).isoformat()
    await database.insert_token(hashed, body.name, expires_at)
    logger.info(f"Token created: {body.name}")
    return TokenCreated(name=body.name, token=raw)


@app.delete("/api/v1/admin/tokens/{name}")
async def revoke_token(name: str, authorization: str = Header(None)):
    await require_master(authorization)
    deleted = await database.delete_token(name)
    if not deleted:
        raise HTTPException(404, "Token not found")
    logger.info(f"Token revoked: {name}")
    return {"status": "revoked"}


@app.post("/api/v1/admin/tokens/{name}/rotate")
async def rotate_token(name: str, authorization: str = Header(None)):
    """Rotate a token: create a new one, old stays valid for 24h grace period."""
    await require_master(authorization)
    result = await database.rotate_token(name)
    if not result:
        raise HTTPException(404, "Token not found")
    new_raw, new_hash = result
    logger.info(f"Token rotated: {name} (old expires in 24h)")
    return {
        "name": name,
        "new_token": new_raw,
        "old_token_expires_in": "24 hours",
        "message": "Old token remains valid for 24 hours. Update your clients to use the new token.",
    }


@app.get("/api/v1/admin/tokens", response_model=list[TokenInfo])
async def list_tokens(authorization: str = Header(None)):
    await require_master(authorization)
    tokens = await database.list_tokens()
    return [TokenInfo(**t) for t in tokens]


# ========== Provider Config ==========

@app.get("/api/v1/admin/providers")
async def get_providers_config(authorization: str = Header(None)):
    await require_master(authorization)
    from nsfw_scanner.scanner import _get_providers
    result = {}
    for p in _get_providers():
        result[p.name] = {"configured": p.is_configured(), "type": "local" if p.name == "nudenet" else "api"}
    return result


# Env var mapping for each provider
_PROVIDER_ENV_MAP = {
    "sightengine": ["SIGHTENGINE_API_USER", "SIGHTENGINE_API_SECRET"],
    "google_vision": ["GOOGLE_VISION_CREDENTIALS"],
    "moderatecontent": ["MODERATECONTENT_API_KEY"],
    "amazon_rekognition": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
    "azure_content_safety": ["AZURE_CONTENT_SAFETY_KEY", "AZURE_CONTENT_SAFETY_ENDPOINT"],
    "picpurify": ["PICPURIFY_API_KEY"],
}


@app.post("/api/v1/admin/providers")
async def update_provider_config(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    updated = []
    # Generic: body keys map to env vars
    env_updates = {
        "sightengine_user": "SIGHTENGINE_API_USER", "sightengine_secret": "SIGHTENGINE_API_SECRET",
        "google_vision_credentials": "GOOGLE_VISION_CREDENTIALS",
        "moderatecontent_key": "MODERATECONTENT_API_KEY",
        "aws_access_key": "AWS_ACCESS_KEY_ID", "aws_secret_key": "AWS_SECRET_ACCESS_KEY", "aws_region": "AWS_REGION",
        "azure_key": "AZURE_CONTENT_SAFETY_KEY", "azure_endpoint": "AZURE_CONTENT_SAFETY_ENDPOINT",
        "picpurify_key": "PICPURIFY_API_KEY",
    }
    for body_key, env_key in env_updates.items():
        if body_key in body:
            os.environ[env_key] = body[body_key]
            await database.save_provider_config(env_key, body[body_key])
            updated.append(env_key)

    return {"status": "ok", "updated": updated, "active": get_active_providers()}


@app.post("/api/v1/admin/providers/disconnect")
async def disconnect_provider(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    provider_name = body.get("provider", "")
    env_vars = _PROVIDER_ENV_MAP.get(provider_name, [])
    for var in env_vars:
        os.environ.pop(var, None)
    await database.delete_provider_config(env_vars)
    logger.info(f"Provider {provider_name} disconnected")
    return {"status": "ok", "provider": provider_name, "active": get_active_providers()}


@app.get("/api/v1/admin/providers/status")
async def providers_status(authorization: str = Header(None)):
    """Get all providers with active/disabled/configured status."""
    await require_token(authorization)
    from nsfw_scanner.scanner import get_all_providers_status
    return get_all_providers_status()


@app.post("/api/v1/admin/providers/disable")
async def disable_provider(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    name = body.get("provider", "")
    if not name:
        raise HTTPException(400, "provider required")
    from nsfw_scanner.scanner import _disabled_providers
    _disabled_providers.add(name)
    # Persist
    await database.save_provider_config("DISABLED_PROVIDERS",
        ",".join(_disabled_providers))
    return {"status": "ok", "disabled": name, "active": get_active_providers()}


@app.post("/api/v1/admin/providers/enable")
async def enable_provider(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    name = body.get("provider", "")
    if not name:
        raise HTTPException(400, "provider required")
    from nsfw_scanner.scanner import _disabled_providers
    _disabled_providers.discard(name)
    await database.save_provider_config("DISABLED_PROVIDERS",
        ",".join(_disabled_providers))
    return {"status": "ok", "enabled": name, "active": get_active_providers()}


@app.post("/api/v1/admin/providers/install")
async def install_provider(body: dict, authorization: str = Header(None)):
    """Install a local provider's pip dependencies."""
    await require_master(authorization)
    name = body.get("provider", "")
    _INSTALL_MAP = {
        "marqo_nsfw": "timm torch",
        "falconsai_nsfw": "transformers torch",
        "freepik_nsfw": "transformers torch",
        "siglip_nsfw": "transformers torch",
        "bumble_private": "tensorflow",
        "nsfwjs": "onnxruntime",
        "deepfake_v2": "transformers torch",
        "yolo_weapons": "ultralytics",
        "detoxify": "detoxify",
        "hate_speech": "transformers torch",
    }
    packages = _INSTALL_MAP.get(name)
    if not packages:
        raise HTTPException(400, f"Unknown provider or no install needed: {name}")
    import subprocess
    try:
        # Install to persistent volume so packages survive rebuilds
        target_dir = "/app/pip_extras"
        os.makedirs(target_dir, exist_ok=True)
        # Ensure target is on Python path
        import site
        if target_dir not in sys.path:
            sys.path.insert(0, target_dir)
            site.addsitedir(target_dir)

        pkg_list = packages.split()
        # Install torch + torchvision CPU-only first if needed
        if "torch" in pkg_list:
            pkg_list.remove("torch")
            torch_result = subprocess.run(
                ["pip", "install", "--no-cache-dir", "--target", target_dir,
                 "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"],
                capture_output=True, text=True, timeout=600,
            )
            if torch_result.returncode != 0:
                return {"status": "error", "output": f"torch install failed: {torch_result.stderr[-500:]}"}
        if pkg_list:
            result = subprocess.run(
                ["pip", "install", "--no-cache-dir", "--target", target_dir] + pkg_list,
                capture_output=True, text=True, timeout=300,
            )
        else:
            result = type('R', (), {'returncode': 0, 'stdout': 'ok', 'stderr': ''})()
        if result.returncode != 0:
            return {"status": "error", "output": result.stderr[-500:]}
        # Force re-check of providers — clear failed import cache
        import sys as _sys
        for mod_name in list(_sys.modules.keys()):
            if any(pkg in mod_name for pkg in packages.split()):
                del _sys.modules[mod_name]
        from nsfw_scanner import scanner
        scanner._providers = None
        return {"status": "ok", "provider": name, "output": result.stdout[-200:],
                "active": get_active_providers()}
    except subprocess.TimeoutExpired:
        return {"status": "error", "output": "Installation timed out (5 min limit)"}


# ========== GitHub Integration ==========

GITHUB_REPO = os.environ.get("GITHUB_REPO", "")


@app.post("/api/v1/report/bug")
async def report_bug(body: dict, authorization: str = Header(None)):
    await require_token(authorization)
    title = body.get("title", "Bug Report")
    description = body.get("description", "")
    labels = body.get("labels", ["bug"])

    if not GITHUB_REPO:
        return {"status": "no_repo", "message": "GITHUB_REPO not configured. Set it in .env"}

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {"status": "no_token", "message": "GITHUB_TOKEN not configured. Set it in .env"}

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                json={"title": title, "body": description, "labels": labels},
                headers={
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    return {"status": "created", "url": data.get("html_url")}
                else:
                    err = await resp.text()
                    return {"status": "error", "code": resp.status, "message": err}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/report/feature")
async def request_feature(body: dict, authorization: str = Header(None)):
    await require_token(authorization)
    body["labels"] = ["enhancement"]
    return await report_bug(body, authorization)


# ========== Domain & DNS Setup ==========

@app.get("/api/v1/admin/domain")
async def get_domain_config(authorization: str = Header(None)):
    await require_master(authorization)
    saved = await database.load_all_provider_config()
    return {
        "domain": saved.get("SAFEEYE_DOMAIN", ""),
        "server_ip": os.popen("curl -s https://api.ipify.org 2>/dev/null || curl -s ifconfig.me 2>/dev/null || echo unknown").read().strip(),
        "port": int(os.environ.get("SCAN_PORT", 1985)),
        "cloudflare_configured": bool(saved.get("CLOUDFLARE_API_TOKEN")),
    }


@app.post("/api/v1/admin/domain/detect-dns")
async def detect_dns(body: dict, authorization: str = Header(None)):
    """Detect DNS provider from domain NS records."""
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    if not domain:
        raise HTTPException(400, "Domain required")

    import subprocess
    try:
        result = subprocess.run(["dig", "+short", "NS", domain], capture_output=True, text=True, timeout=10)
        ns_records = [line.strip().lower() for line in result.stdout.strip().split("\n") if line.strip()]

        # Also check parent domain if subdomain
        parts = domain.split(".")
        if len(parts) > 2:
            parent = ".".join(parts[-2:])
            result2 = subprocess.run(["dig", "+short", "NS", parent], capture_output=True, text=True, timeout=10)
            ns_records += [line.strip().lower() for line in result2.stdout.strip().split("\n") if line.strip()]

        provider = "unknown"
        for ns in ns_records:
            if "cloudflare" in ns:
                provider = "cloudflare"
                break
            elif "awsdns" in ns or "amazonaws" in ns:
                provider = "route53"
                break
            elif "google" in ns or "googledomains" in ns:
                provider = "google"
                break
            elif "domaincontrol" in ns or "godaddy" in ns:
                provider = "godaddy"
                break
            elif "namecheap" in ns or "registrar-servers" in ns:
                provider = "namecheap"
                break

        return {
            "domain": domain,
            "ns_records": ns_records,
            "provider": provider,
            "auto_dns_supported": provider == "cloudflare",
        }
    except Exception as e:
        return {"domain": domain, "error": str(e), "provider": "unknown", "auto_dns_supported": False}


@app.post("/api/v1/admin/domain/setup-dns")
async def setup_dns_auto(body: dict, authorization: str = Header(None)):
    """Auto-add DNS A record via Cloudflare API."""
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    cf_token = body.get("cloudflare_token", "").strip()

    if not domain or not cf_token:
        raise HTTPException(400, "Domain and Cloudflare token required")

    # Save token for future use
    await database.save_provider_config("CLOUDFLARE_API_TOKEN", cf_token)
    await database.save_provider_config("SAFEEYE_DOMAIN", domain)

    server_ip = os.popen("curl -s ifconfig.me 2>/dev/null").read().strip()

    import aiohttp
    try:
        headers = {"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}

        # Find the zone
        parts = domain.split(".")
        zone_name = ".".join(parts[-2:])

        async with aiohttp.ClientSession() as session:
            # Get zone ID
            async with session.get(
                f"https://api.cloudflare.com/client/v4/zones?name={zone_name}",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if not data.get("success") or not data.get("result"):
                    return {"status": "error", "message": f"Zone not found for {zone_name}. Check your Cloudflare token permissions."}
                zone_id = data["result"][0]["id"]

            # Check if record exists
            record_name = domain
            async with session.get(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A&name={record_name}",
                headers=headers,
            ) as resp:
                data = await resp.json()
                existing = data.get("result", [])

            if existing:
                # Update existing record
                record_id = existing[0]["id"]
                async with session.put(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
                    headers=headers,
                    json={"type": "A", "name": record_name, "content": server_ip, "proxied": True},
                ) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        return {"status": "ok", "action": "updated", "domain": domain, "ip": server_ip}
            else:
                # Create new record
                async with session.post(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
                    headers=headers,
                    json={"type": "A", "name": record_name, "content": server_ip, "proxied": True},
                ) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        return {"status": "ok", "action": "created", "domain": domain, "ip": server_ip}

            return {"status": "error", "message": str(data.get("errors", "Unknown error"))}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/admin/domain/setup-direct")
async def setup_direct(body: dict, authorization: str = Header(None)):
    """
    Direct domain setup — no nginx needed.
    Just DNS pointing to server IP + port. For VPS users without nginx.
    """
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    if not domain:
        raise HTTPException(400, "Domain required")

    await database.save_provider_config("SAFEEYE_DOMAIN", domain)
    port = int(os.environ.get("SCAN_PORT", 1985))
    server_ip = os.popen("curl -s https://api.ipify.org 2>/dev/null || echo unknown").read().strip()

    return {
        "status": "ok",
        "domain": domain,
        "ip": server_ip,
        "port": port,
        "access_url": f"http://{domain}:{port}/dashboard",
        "note": "Point your domain A record to the server IP. Access via http://domain:port/dashboard",
    }


@app.post("/api/v1/admin/domain/setup-nginx")
async def setup_nginx(body: dict, authorization: str = Header(None)):
    """Run the nginx setup script for the domain."""
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    if not domain:
        raise HTTPException(400, "Domain required")

    await database.save_provider_config("SAFEEYE_DOMAIN", domain)
    port = int(os.environ.get("SCAN_PORT", 1985))

    import subprocess
    script = os.path.join(os.path.dirname(__file__), "setup_domain.sh")

    # The script needs sudo — check if it can run
    try:
        result = subprocess.run(
            ["sudo", "-n", script, domain, str(port)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return {"status": "ok", "domain": domain, "output": result.stdout[-500:]}
        else:
            return {"status": "error", "message": result.stderr[-500:],
                    "manual": f"Run manually: sudo {script} {domain} {port}"}
    except Exception as e:
        return {"status": "manual_required",
                "message": str(e),
                "command": f"sudo {script} {domain} {port}"}


def _find_nginx_config(domain: str) -> dict:
    """Search for nginx config file matching this domain. Returns {found, path, searched}."""
    import glob as _glob
    searched = []

    # 1. Direct name match (with and without .conf)
    direct_paths = [
        f"/etc/nginx/sites-enabled/{domain}",
        f"/etc/nginx/sites-enabled/{domain}.conf",
        f"/etc/nginx/sites-available/{domain}",
        f"/etc/nginx/sites-available/{domain}.conf",
        f"/etc/nginx/conf.d/{domain}.conf",
        f"/etc/nginx/conf.d/{domain}",
    ]
    for p in direct_paths:
        searched.append(p)
        if os.path.isfile(p):
            return {"found": True, "path": p, "searched": searched}

    # 2. Scan ALL files in nginx dirs — match by server_name directive or filename containing domain
    scan_dirs = ["/etc/nginx/sites-enabled", "/etc/nginx/sites-available", "/etc/nginx/conf.d"]
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            full = os.path.join(d, f)
            if not os.path.isfile(full):
                continue
            searched.append(full)

            # Check if filename contains domain or domain part
            if domain in f or domain.split(".")[0] in f:
                return {"found": True, "path": full, "searched": searched}

            # Check file content for server_name
            try:
                content = open(full).read(4096)  # Read first 4KB
                if f"server_name {domain}" in content or f"server_name .{domain}" in content:
                    return {"found": True, "path": full, "searched": searched}
                # Also check if domain appears anywhere (e.g. in comments, ssl cert paths)
                if domain in content:
                    return {"found": True, "path": full, "searched": searched}
            except (OSError, PermissionError):
                pass

    return {"found": False, "path": None, "searched": searched}


@app.post("/api/v1/admin/domain/detect-nginx")
async def detect_nginx_config(body: dict, authorization: str = Header(None)):
    """Step 1: Find nginx config file for a domain. User confirms or provides custom path."""
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    if not domain:
        raise HTTPException(400, "Domain required")

    result = _find_nginx_config(domain)
    if result["found"]:
        return {"status": "found", "config_file": result["path"], "searched": result["searched"]}
    else:
        return {"status": "not_found", "searched": result["searched"], "message": f"No nginx config found for {domain}"}


@app.post("/api/v1/admin/domain/setup-path")
async def setup_path_auto(body: dict, authorization: str = Header(None)):
    """Step 2: Add location block to a confirmed nginx config file."""
    await require_master(authorization)
    domain = body.get("domain", "").strip()
    path_prefix = body.get("path", "").strip().strip("/")
    config_file = body.get("config_file", "").strip()  # User-confirmed or custom path
    port = int(os.environ.get("SCAN_PORT", 1985))

    if not domain or not path_prefix:
        raise HTTPException(400, "Domain and path required")

    # If no config_file provided, auto-detect
    if not config_file:
        result = _find_nginx_config(domain)
        if not result["found"]:
            return {
                "status": "not_found",
                "message": f"Nginx config for {domain} not found",
                "searched": result["searched"],
                "snippet": _nginx_location_block(path_prefix, port),
            }
        config_file = result["path"]

    # Validate the file exists
    if not os.path.exists(config_file):
        return {"status": "not_found", "message": f"File not found: {config_file}"}

    # Read current config
    try:
        with open(config_file) as f:
            content = f.read()
    except PermissionError:
        return {
            "status": "permission_denied",
            "config_file": config_file,
            "snippet": _nginx_location_block(path_prefix, port),
            "command": f"sudo nano {config_file}",
        }

    if f"location /{path_prefix}" in content:
        return {"status": "exists", "message": f"Location /{path_prefix} already exists in {config_file}", "config_file": config_file}

    # Insert before the last closing brace (end of server block)
    snippet = _nginx_location_block(path_prefix, port)
    lines = content.rstrip().rsplit("}", 1)
    if len(lines) == 2:
        new_content = lines[0] + "\n" + snippet + "\n}\n"
    else:
        return {"status": "parse_error", "message": "Could not find server block closing brace", "config_file": config_file, "snippet": snippet}

    # Write back (needs sudo)
    import subprocess
    try:
        proc = subprocess.run(
            ["sudo", "-n", "tee", config_file],
            input=new_content.encode(), capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            return {
                "status": "permission_denied",
                "config_file": config_file,
                "snippet": snippet,
                "command": f"sudo nano {config_file}",
            }

        # Test and reload
        test = subprocess.run(["sudo", "-n", "nginx", "-t"], capture_output=True, text=True, timeout=10)
        if test.returncode != 0:
            return {"status": "config_error", "message": test.stderr, "config_file": config_file}

        subprocess.run(["sudo", "-n", "nginx", "-s", "reload"], capture_output=True, timeout=10)

        await database.save_provider_config("SAFEEYE_DOMAIN", f"{domain}/{path_prefix}")
        return {"status": "ok", "config_file": config_file, "path": f"/{path_prefix}", "url": f"https://{domain}/{path_prefix}/dashboard"}

    except Exception as e:
        return {"status": "manual_required", "message": str(e), "config_file": config_file, "snippet": snippet}


def _nginx_location_block(path: str, port: int) -> str:
    return f"""    location /{path} {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 50M;
    }}"""


# ========== Telegram Verification ==========

import secrets as _secrets

# In-memory pending verifications (short-lived)
_pending_verifications: dict = {}


@app.post("/api/v1/admin/telegram/start-verify")
async def start_telegram_verify(body: dict, authorization: str = Header(None)):
    """
    Start Telegram bot verification.
    User provides their Telegram bot token. We generate a code,
    send it via the bot, and wait for confirmation.
    """
    await require_master(authorization)
    bot_token = body.get("bot_token", "").strip()
    if not bot_token:
        raise HTTPException(400, "bot_token required")

    # Generate 6-digit verification code
    code = str(random.randint(100000, 999999))
    _pending_verifications[code] = {"bot_token": bot_token, "verified": False, "created": datetime.utcnow().isoformat()}

    # Try to get bot info to validate token
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    return {"status": "error", "message": "Invalid bot token"}
                bot_info = data["result"]
    except Exception as e:
        return {"status": "error", "message": f"Failed to validate bot token: {e}"}

    # Save bot token for later
    await database.save_provider_config("TELEGRAM_BOT_TOKEN", bot_token)
    await database.save_provider_config("TELEGRAM_BOT_USERNAME", bot_info.get("username", ""))

    return {
        "status": "ok",
        "code": code,
        "bot_username": bot_info.get("username"),
        "bot_name": bot_info.get("first_name"),
        "instructions": f"Send the code {code} to @{bot_info.get('username')} in Telegram to verify.",
    }


@app.post("/api/v1/admin/telegram/check-verify")
async def check_telegram_verify(body: dict, authorization: str = Header(None)):
    """Check if verification code was confirmed via Telegram."""
    await require_master(authorization)
    code = body.get("code", "").strip()

    if code not in _pending_verifications:
        return {"status": "error", "message": "Invalid or expired code"}

    pending = _pending_verifications[code]
    bot_token = pending["bot_token"]

    # Poll Telegram for updates containing the code
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"timeout": 0, "limit": 20},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    if text.strip() == code:
                        chat_id = msg["chat"]["id"]
                        user = msg.get("from", {})

                        # Save verified admin info
                        await database.save_provider_config("TELEGRAM_ADMIN_CHAT_ID", str(chat_id))
                        await database.save_provider_config("TELEGRAM_ADMIN_USER", user.get("username", str(chat_id)))
                        await database.save_provider_config("TELEGRAM_VERIFIED", "true")

                        # Send confirmation
                        await session.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": "✅ SafeEyes verified! You'll receive alerts here."},
                        )

                        del _pending_verifications[code]
                        return {
                            "status": "verified",
                            "admin_user": user.get("username") or user.get("first_name"),
                            "chat_id": chat_id,
                        }

        return {"status": "waiting", "message": "Code not yet received. Send it to the bot."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/admin/telegram/status")
async def telegram_status(authorization: str = Header(None)):
    """Get Telegram bot verification status."""
    await require_master(authorization)
    saved = await database.load_all_provider_config()
    return {
        "verified": saved.get("TELEGRAM_VERIFIED") == "true",
        "bot_username": saved.get("TELEGRAM_BOT_USERNAME", ""),
        "admin_user": saved.get("TELEGRAM_ADMIN_USER", ""),
    }


import random


# ========== Sandbox (test tokens) ==========

@app.post("/api/v1/sandbox/token")
async def create_sandbox_token(authorization: str = Header(None)):
    """Create a temporary sandbox token for API testing. Scans work but don't persist."""
    await require_token(authorization)
    raw, hashed = auth.generate_token()
    await database.insert_token(hashed, f"_sandbox_{raw[:6]}", None)
    # Mark as sandbox in DB
    await database.save_provider_config(f"SANDBOX_{hashed}", "true")
    return {"token": raw, "note": "Sandbox token — scans work but results are not saved"}


async def _is_sandbox_token(token_hash: str) -> bool:
    saved = await database.load_all_provider_config()
    return saved.get(f"SANDBOX_{token_hash}") == "true"


@app.post("/api/v1/sandbox/scan")
async def sandbox_scan(
    file: UploadFile = File(...),
    authorization: str = Header(None),
):
    """Scan a file in sandbox mode — runs all providers but doesn't save results."""
    token_data = await require_token(authorization)

    suffix = os.path.splitext(file.filename or "upload")[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=suffix, delete=False)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        result = await scan_file(tmp.name)
        # Don't persist to DB — sandbox mode
        return {
            "scan_id": result.scan_id,
            "result": result.model_dump(),
            "timestamp": datetime.utcnow().isoformat(),
            "sandbox": True,
        }
    finally:
        os.unlink(tmp.name)


# ========== Community (public, UUID-tracked) ==========

import uuid as _uuid

@app.get("/api/v1/community")
async def list_community(type: str = Query(None), sort: str = Query("votes"), limit: int = Query(50)):
    return await database.list_community_reports(type, sort, limit)


@app.post("/api/v1/community")
async def create_community_report(body: dict):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "Title required")
    report_id = _uuid.uuid4().hex[:12]
    return await database.insert_community_report(
        report_id,
        body.get("type", "feature"),
        title,
        body.get("description", ""),
        body.get("device_uuid", "anonymous"),
    )


@app.get("/api/v1/community/{report_id}")
async def get_community_report(report_id: str):
    report = await database.get_community_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@app.post("/api/v1/community/{report_id}/vote")
async def vote_community(report_id: str, body: dict):
    device_uuid = body.get("device_uuid", "")
    if not device_uuid:
        raise HTTPException(400, "device_uuid required")
    report = await database.get_community_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return await database.vote_community_report(report_id, device_uuid)


# ========== Gossip P2P ==========

from fastapi import WebSocket

@app.websocket("/api/v1/gossip/ws")
async def gossip_ws(ws: WebSocket):
    """WebSocket endpoint for gossip P2P connections."""
    await ws.accept()
    if not gossip_node.enabled:
        await ws.close(code=4003, reason="Gossip disabled")
        return
    await gossip_node.handle_incoming_ws(ws)


@app.get("/api/v1/gossip/status")
async def gossip_status(authorization: str = Header(None)):
    await require_token(authorization)
    return gossip_node.get_status()


@app.post("/api/v1/gossip/configure")
async def gossip_configure(body: dict, authorization: str = Header(None)):
    """Enable/disable P2P network. Zero-config — key auto-generated."""
    await require_master(authorization)
    enabled = body.get("enabled", False)

    await database.save_provider_config("GOSSIP_ENABLED", "1" if enabled else "0")

    await gossip_node.stop()
    if enabled:
        gossip_node.configure(True)
        gossip_node.on_hash(lambda rec: database.import_hash_metadata([rec], "gossip"))
        await database.save_provider_config("GOSSIP_SERVER_ID", gossip_node.server_id)
        await database.save_provider_config("GOSSIP_SERVER_KEY", gossip_node.server_key)
        await gossip_node.start()

    return {"status": "ok", "gossip": gossip_node.get_status()}


@app.post("/api/v1/gossip/add-peer")
async def gossip_add_peer(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url required")
    # Save to config
    saved = await database.load_all_provider_config()
    existing = [p for p in saved.get("GOSSIP_PEERS", "").split(",") if p.strip()]
    if url not in existing:
        existing.append(url)
        await database.save_provider_config("GOSSIP_PEERS", ",".join(existing))
    # Add to running node
    from nsfw_scanner.gossip import Peer
    if url not in gossip_node.peers:
        gossip_node.peers[url] = Peer(url=url, secret=gossip_node.shared_secret)
        if gossip_node.enabled:
            task = asyncio.create_task(gossip_node._connect_loop(gossip_node.peers[url]))
            gossip_node._tasks.append(task)
    return {"status": "ok", "peers": gossip_node.get_status()["peers"]}


@app.post("/api/v1/gossip/remove-peer")
async def gossip_remove_peer(body: dict, authorization: str = Header(None)):
    await require_master(authorization)
    url = body.get("url", "").strip()
    if url in gossip_node.peers:
        peer = gossip_node.peers.pop(url)
        if peer.ws:
            try:
                await peer.ws.close()
            except Exception:
                pass
    saved = await database.load_all_provider_config()
    existing = [p for p in saved.get("GOSSIP_PEERS", "").split(",") if p.strip() and p.strip() != url]
    await database.save_provider_config("GOSSIP_PEERS", ",".join(existing))
    return {"status": "ok", "peers": gossip_node.get_status()["peers"]}


@app.get("/api/v1/network/stats")
async def network_stats():
    """Network stats — no auth required."""
    status = gossip_node.get_status()
    return {
        "active_servers": status["network_count"] + (1 if status["enabled"] else 0),
        "servers": status["network_servers"],
        "this_server": status["server_id"] if status["enabled"] else None,
    }


# Registry: other servers register here, we maintain the list
_registered_servers: dict[str, dict] = {}  # server_id -> {last_seen, signature}

@app.post("/api/v1/network/register")
async def register_server(body: dict):
    """Servers ping this to register. No auth — just server_id + signature."""
    sid = body.get("server_id", "")
    sig = body.get("signature", "")
    if not sid or not sig:
        raise HTTPException(400, "server_id and signature required")
    _registered_servers[sid] = {"last_seen": _time_mod.time(), "signature": sig}
    # Clean old entries (not seen in 15 min)
    cutoff = _time_mod.time() - 900
    for k in list(_registered_servers):
        if _registered_servers[k]["last_seen"] < cutoff:
            del _registered_servers[k]
    return {"status": "ok", "active": len(_registered_servers)}


@app.get("/api/v1/network/registry")
async def get_registry():
    """Get list of active servers. No auth."""
    cutoff = _time_mod.time() - 900
    active = [{"server_id": k, "last_seen": int(v["last_seen"])}
              for k, v in _registered_servers.items() if v["last_seen"] >= cutoff]
    return {"servers": active, "count": len(active)}


# ========== Extension Pairing ==========

_pairing_codes: dict[str, dict] = {}  # code -> {token, expires}

@app.post("/api/v1/extension/pair/create")
async def create_pairing_code(authorization: str = Header(None)):
    """Generate a 6-digit pairing code for the browser extension. Returns code valid for 5 min."""
    token_data = await require_token(authorization)
    raw_token = authorization.removeprefix("Bearer ").strip()
    code = f"{_secrets.randbelow(900000) + 100000}"
    _pairing_codes[code] = {"token": raw_token, "expires": _time.time() + 300}
    return {"code": code, "expires_in": 300}


@app.post("/api/v1/extension/pair/redeem")
async def redeem_pairing_code(body: dict):
    """Redeem a pairing code to get the API token. No auth required."""
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "code required")
    entry = _pairing_codes.get(code)
    if not entry:
        raise HTTPException(401, "Invalid or expired code")
    if _time.time() > entry["expires"]:
        del _pairing_codes[code]
        raise HTTPException(401, "Code expired")
    token = entry["token"]
    del _pairing_codes[code]
    return {"status": "paired", "token": token}


# ========== Telegram Auth (for domain access) ==========

import secrets as _secrets
import time as _time

_tg_auth_codes: dict[str, dict] = {}  # username -> {code, expires, chat_id}

@app.get("/api/v1/auth/mode")
async def auth_mode(request: Request):
    """Detect if login is required based on access method."""
    host = request.headers.get("host", "")
    # Local access: no auth needed
    if any(host.startswith(h) for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")):
        # Return master token for local access
        master = os.environ.get("SCAN_API_MASTER_TOKEN", "")
        return {"mode": "local", "token": master}
    # Domain access: check if Telegram bot is configured
    saved = await database.load_all_provider_config()
    if saved.get("TELEGRAM_BOT_TOKEN") and saved.get("TELEGRAM_VERIFIED") == "true":
        return {"mode": "telegram", "bot_username": saved.get("TELEGRAM_BOT_USERNAME", "")}
    # Domain but no Telegram: fall back to token login
    return {"mode": "token"}


@app.post("/api/v1/auth/telegram/request")
async def telegram_auth_request(body: dict):
    """Send auth code to user via Telegram bot."""
    username = body.get("username", "").strip().lstrip("@").lower()
    if not username:
        raise HTTPException(400, "username required")
    saved = await database.load_all_provider_config()
    bot_token = saved.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise HTTPException(503, "Telegram bot not configured")

    # Generate 6-digit code
    code = f"{_secrets.randbelow(900000) + 100000}"
    _tg_auth_codes[username] = {"code": code, "expires": _time.time() + 300}  # 5 min

    # Find chat_id by username from recent updates
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            # Get updates to find chat_id for this username
            async with session.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"limit": 100, "timeout": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()

            chat_id = None
            for update in data.get("result", []):
                msg = update.get("message", {})
                user = msg.get("from", {})
                if user.get("username", "").lower() == username:
                    chat_id = msg["chat"]["id"]
                    break

            if not chat_id:
                # Check if this is the admin user
                admin_user = saved.get("TELEGRAM_ADMIN_USER", "").lower()
                admin_chat = saved.get("TELEGRAM_ADMIN_CHAT_ID")
                if username == admin_user and admin_chat:
                    chat_id = admin_chat
                else:
                    raise HTTPException(404,
                        f"User @{username} not found. Send /start to the bot first.")

            _tg_auth_codes[username]["chat_id"] = str(chat_id)

            # Send code via Telegram
            async with session.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"🛡️ SafeEyes login code:\n\n🔑 {code}\n\nExpires in 5 minutes."},
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(502, "Failed to send Telegram message")
    except aiohttp.ClientError as e:
        raise HTTPException(502, f"Telegram API error: {e}")

    return {"status": "sent", "username": username}


@app.post("/api/v1/auth/telegram/verify")
async def telegram_auth_verify(body: dict):
    """Verify the code and return a session token."""
    username = body.get("username", "").strip().lstrip("@").lower()
    code = body.get("code", "").strip()
    if not username or not code:
        raise HTTPException(400, "username and code required")

    entry = _tg_auth_codes.get(username)
    if not entry:
        raise HTTPException(401, "No pending code for this user. Request a new one.")
    if _time.time() > entry["expires"]:
        del _tg_auth_codes[username]
        raise HTTPException(401, "Code expired. Request a new one.")
    if entry["code"] != code:
        raise HTTPException(401, "Wrong code")

    del _tg_auth_codes[username]

    # Return master token for verified Telegram users
    master = os.environ.get("SCAN_API_MASTER_TOKEN", "")
    return {"status": "verified", "token": master, "username": username}


# ========== Hash Metadata (client-side pre-scan) ==========

@app.get("/api/v1/metadata/hashes")
async def export_metadata():
    """Export hash→result metadata for client-side matching.
    Clients download this once, cache locally, and check before uploading."""
    enabled = (await database.load_all_provider_config()).get("METADATA_SHARING", "0")
    if enabled != "1":
        raise HTTPException(403, "Metadata sharing is disabled on this server")
    data = await database.export_hash_metadata()
    return JSONResponse(data, headers={
        "Cache-Control": "public, max-age=3600",
    })


@app.post("/api/v1/metadata/sync")
async def sync_metadata(body: dict, _=Depends(require_master)):
    """Import hash metadata from another SafeEyes server. Master token required."""
    source = body.get("source", "unknown")
    records = body.get("records", [])
    if not records:
        raise HTTPException(400, "No records provided")
    await database.import_hash_metadata(records, source)
    return {"status": "ok", "imported": len(records)}


@app.post("/api/v1/metadata/subscribe")
async def subscribe_metadata(body: dict, _=Depends(require_master)):
    """Subscribe to another SafeEyes server's metadata. Fetches and imports."""
    import aiohttp
    server_url = body.get("url", "").rstrip("/")
    if not server_url:
        raise HTTPException(400, "url required")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{server_url}/api/v1/metadata/hashes", timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise HTTPException(502, f"Remote server returned {resp.status}")
                records = await resp.json()
        await database.import_hash_metadata(records, server_url)
        return {"status": "ok", "fetched": len(records), "source": server_url}
    except aiohttp.ClientError as e:
        raise HTTPException(502, f"Failed to reach server: {e}")


@app.get("/api/v1/metadata/check/{phash}")
async def check_hash(phash: str):
    """Quick check: does this pHash exist in our database? No auth needed."""
    similar = await database.find_similar_by_phash(phash, threshold=3, limit=1)
    if similar:
        hit = similar[0]
        return {"match": True, "is_nsfw": bool(hit["is_nsfw"]), "confidence": hit["confidence"],
                "labels": hit["labels"], "distance": hit["hamming_distance"]}
    return {"match": False}


# ========== Public Demo Endpoint ==========

@app.post("/api/v1/demo/scan")
async def demo_scan(
    file: UploadFile = File(None),
    url: str = Query(None),
):
    """Public demo endpoint — no auth needed. Runs scan but doesn't persist."""
    if not file and not url:
        raise HTTPException(400, "Provide file or url parameter")

    if url:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise HTTPException(400, f"HTTP {resp.status}")
                    content = await resp.read()
                    ct = resp.headers.get("Content-Type", "")
        except aiohttp.ClientError as e:
            raise HTTPException(400, str(e))

        _check_file_size(content)

        # Reject non-media — try og:image first
        if ct and not any(t in ct for t in ["image", "video", "octet-stream"]):
            og_url = _extract_og_image(content.decode("utf-8", errors="ignore")) if "html" in ct else None
            if og_url:
                try:
                    async with aiohttp.ClientSession() as s2:
                        async with s2.get(og_url, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                            if r2.status == 200:
                                content = await r2.read()
                                ct = r2.headers.get("Content-Type", "image/jpeg")
                                logger.info(f"Demo: extracted og:image: {og_url[:80]}")
                            else:
                                raise HTTPException(400, f"og:image failed: HTTP {r2.status}")
                except aiohttp.ClientError as e:
                    raise HTTPException(400, f"og:image failed: {e}")
            else:
                raise HTTPException(400,
                    f"URL is not an image or video (Content-Type: {ct}). "
                    "No og:image found. Provide a direct link to a media file.")

        ext = ".jpg"
        if "png" in ct: ext = ".png"
        elif "webp" in ct: ext = ".webp"
        elif "video" in ct or "mp4" in ct: ext = ".mp4"

        tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=ext, delete=False)
        tmp.write(content)
        tmp.close()
    else:
        suffix = os.path.splitext(file.filename or "upload")[1] or ".jpg"
        tmp = tempfile.NamedTemporaryFile(dir=TEMP_DIR, suffix=suffix, delete=False)
        demo_content = await file.read()
        _check_file_size(demo_content)
        tmp.write(demo_content)
        tmp.close()

    try:
        result = await scan_file(tmp.name)
        return {
            "scan_id": result.scan_id,
            "result": result.model_dump(),
            "timestamp": datetime.utcnow().isoformat(),
            "demo": True,
        }
    finally:
        os.unlink(tmp.name)


# ========== Dashboard ==========

@app.get("/dashboard")
async def dashboard():
    from fastapi.responses import HTMLResponse
    with open(os.path.join(STATIC_DIR, "dashboard.html"), "r") as f:
        content = f.read()
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})


# ========== Auto-Deploy ==========

def _run_deploy():
    """Run git pull + docker compose rebuild. Works from inside container with mounted docker.sock."""
    import subprocess as _sp
    repo_dir = os.path.dirname(os.path.dirname(__file__))
    log = "/tmp/safeeye_deploy.log"

    with open(log, "a") as lf:
        lf.write(f"\n{'='*40}\n{datetime.now().isoformat()} — Deploy started\n")

        # Git pull (works if .git is available via volume or we're on host)
        try:
            r = _sp.run(["git", "pull", "origin", "main"], capture_output=True, text=True, cwd=repo_dir, timeout=30)
            lf.write(f"git pull: {r.stdout}\n{r.stderr}\n")
        except Exception as e:
            lf.write(f"git pull failed: {e}\n")

        # If docker socket is mounted, rebuild
        try:
            r = _sp.run(["docker", "compose", "up", "-d", "--build", "safeeye"],
                        capture_output=True, text=True, cwd=repo_dir, timeout=120)
            lf.write(f"docker compose: {r.stdout}\n{r.stderr}\n")
        except FileNotFoundError:
            # No docker CLI in container — try just git pull (static files update via volume mount)
            lf.write("docker CLI not available — static files updated via volume mount\n")
        except Exception as e:
            lf.write(f"docker compose failed: {e}\n")

        lf.write(f"{datetime.now().isoformat()} — Deploy finished\n")


@app.post("/api/v1/admin/deploy")
async def trigger_deploy(authorization: str = Header(None)):
    """Trigger auto-deploy: git pull + rebuild."""
    await require_master(authorization)
    import threading
    threading.Thread(target=_run_deploy, daemon=True).start()
    return {"status": "deploying", "log": "/tmp/safeeye_deploy.log"}


@app.get("/api/v1/admin/deploy/status")
async def deploy_status(authorization: str = Header(None)):
    """Check deploy log."""
    await require_master(authorization)
    try:
        with open("/tmp/safeeye_deploy.log") as f:
            lines = f.readlines()
        return {"status": "ok", "log": "".join(lines[-30:])}
    except FileNotFoundError:
        return {"status": "ok", "log": "No deploys yet"}


@app.post("/api/v1/webhook/github")
async def github_webhook(body: dict):
    """GitHub webhook — auto-deploy on push to main."""
    if body.get("ref") != "refs/heads/main":
        return {"status": "ignored"}
    import threading
    threading.Thread(target=_run_deploy, daemon=True).start()
    logger.info("GitHub webhook: deploy triggered")
    return {"status": "deploying"}


# ========== Analytics ==========

_api_usage: dict = {}   # endpoint -> count
_visitor_log: list = [] # last 500 visits (admin only)

@app.middleware("http")
async def track_analytics(request: Request, call_next):
    path = request.url.path
    _api_usage[path] = _api_usage.get(path, 0) + 1
    # Log visitor details (for admin analytics only)
    if len(_visitor_log) > 500:
        _visitor_log.pop(0)
    _visitor_log.append({
        "path": path,
        "method": request.method,
        "ip": request.client.host if request.client else "unknown",
        "ua": request.headers.get("user-agent", "")[:100],
        "referer": request.headers.get("referer", "")[:200],
        "ts": datetime.utcnow().isoformat(),
    })
    response = await call_next(request)
    return response


@app.get("/api/v1/admin/analytics")
async def get_analytics(authorization: str = Header(None)):
    """Usage analytics: page views, API calls, GitHub stats."""
    await require_master(authorization)

    # GitHub stats (stars, forks, downloads)
    gh_stats = {}
    try:
        import aiohttp as _aio
        async with _aio.ClientSession() as session:
            async with session.get(
                "https://api.github.com/repos/Dandona100/SafeEyes",
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=_aio.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    gh_stats = {
                        "stars": data.get("stargazers_count", 0),
                        "forks": data.get("forks_count", 0),
                        "watchers": data.get("watchers_count", 0),
                        "open_issues": data.get("open_issues_count", 0),
                    }
    except Exception:
        pass

    # Docker pulls (from scan count as proxy)
    db = await database.get_db()
    try:
        total_scans = await db.execute_fetchall("SELECT COUNT(*) FROM scan_history")
        total_tokens = await db.execute_fetchall("SELECT COUNT(*) FROM api_tokens")
        total_community = await db.execute_fetchall("SELECT COUNT(*) FROM community_reports")
        total_votes = await db.execute_fetchall("SELECT SUM(vote_count) FROM community_reports")
    finally:
        await db.close()

    # API usage (public-safe)
    top_endpoints = sorted(_api_usage.items(), key=lambda x: -x[1])[:15]

    # Unique visitors (by IP)
    unique_ips = set(v["ip"] for v in _visitor_log if v.get("ip"))

    # Recent visitors (last 20)
    recent = _visitor_log[-20:][::-1]

    # Referrers
    referrers = {}
    for v in _visitor_log:
        ref = v.get("referer", "")
        if ref and "lhflow" not in ref and "localhost" not in ref:
            referrers[ref] = referrers.get(ref, 0) + 1
    top_referrers = sorted(referrers.items(), key=lambda x: -x[1])[:10]

    return {
        "github": gh_stats,
        "usage": {
            "total_scans": total_scans[0][0] if total_scans else 0,
            "total_tokens": total_tokens[0][0] if total_tokens else 0,
            "community_reports": total_community[0][0] if total_community else 0,
            "community_votes": total_votes[0][0] if total_votes and total_votes[0][0] else 0,
        },
        "visitors": {
            "total_requests": sum(_api_usage.values()),
            "unique_ips": len(unique_ips),
            "logged": len(_visitor_log),
        },
        "api_usage": dict(top_endpoints),
        "top_referrers": dict(top_referrers),
        "recent_visitors": recent,
        "uptime_seconds": round(_time_mod.monotonic() - _start_time, 1),
    }


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint. No auth required for scraping."""
    from fastapi.responses import Response

    active = get_active_providers()
    uptime = round(_time_mod.monotonic() - _start_time, 1)

    # Count tokens from DB
    try:
        tokens = await database.list_tokens()
        token_count = len(tokens)
    except Exception:
        token_count = 0

    lines = []

    # safeeye_scans_total
    lines.append("# HELP safeeye_scans_total Total number of scans by result")
    lines.append("# TYPE safeeye_scans_total counter")
    lines.append(f'safeeye_scans_total{{result="nsfw"}} {_metrics["scans_total_nsfw"]}')
    lines.append(f'safeeye_scans_total{{result="safe"}} {_metrics["scans_total_safe"]}')

    # safeeye_scan_duration_seconds histogram buckets
    durations = _metrics["scan_durations"]
    buckets = [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
    total_count = len(durations)
    total_sum = sum(durations)
    lines.append("# HELP safeeye_scan_duration_seconds Scan duration in seconds")
    lines.append("# TYPE safeeye_scan_duration_seconds histogram")
    for b in buckets:
        count = sum(1 for d in durations if d <= b)
        lines.append(f'safeeye_scan_duration_seconds_bucket{{le="{b}"}} {count}')
    lines.append(f'safeeye_scan_duration_seconds_bucket{{le="+Inf"}} {total_count}')
    lines.append(f"safeeye_scan_duration_seconds_sum {total_sum:.4f}")
    lines.append(f"safeeye_scan_duration_seconds_count {total_count}")

    # safeeye_provider_scans_total
    lines.append("# HELP safeeye_provider_scans_total Total scans per provider")
    lines.append("# TYPE safeeye_provider_scans_total counter")
    for provider, count in sorted(_metrics["provider_scans"].items()):
        lines.append(f'safeeye_provider_scans_total{{provider="{provider}"}} {count}')

    # safeeye_provider_errors_total
    lines.append("# HELP safeeye_provider_errors_total Total errors per provider")
    lines.append("# TYPE safeeye_provider_errors_total counter")
    for provider, count in sorted(_metrics["provider_errors"].items()):
        lines.append(f'safeeye_provider_errors_total{{provider="{provider}"}} {count}')

    # safeeye_active_providers
    lines.append("# HELP safeeye_active_providers Number of active providers")
    lines.append("# TYPE safeeye_active_providers gauge")
    lines.append(f"safeeye_active_providers {len(active)}")

    # safeeye_uptime_seconds
    lines.append("# HELP safeeye_uptime_seconds Service uptime in seconds")
    lines.append("# TYPE safeeye_uptime_seconds gauge")
    lines.append(f"safeeye_uptime_seconds {uptime}")

    # safeeye_tokens_total
    lines.append("# HELP safeeye_tokens_total Total number of API tokens")
    lines.append("# TYPE safeeye_tokens_total gauge")
    lines.append(f"safeeye_tokens_total {token_count}")

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    from nsfw_scanner.scanner import _get_providers

    # DB connectivity check
    db_status = "ok"
    try:
        db = await database.get_db()
        try:
            await db.execute_fetchall("SELECT 1")
        finally:
            await db.close()
    except Exception as e:
        db_status = f"error: {e}"

    # NudeNet model loaded check
    nudenet_status = "not_loaded"
    try:
        from nsfw_scanner.providers.nudenet_provider import _get_detector
        det = _get_detector()
        nudenet_status = "ok" if det is not None else "not_loaded"
    except Exception:
        nudenet_status = "not_loaded"

    # Provider status (never let a broken provider crash the health check)
    providers_status = {}
    for p in _get_providers():
        try:
            providers_status[p.name] = "ok" if p.is_configured() else "not_configured"
        except Exception:
            providers_status[p.name] = "error"
    # Override nudenet with model-loaded status
    if "nudenet" in providers_status and providers_status["nudenet"] == "ok":
        providers_status["nudenet"] = nudenet_status

    uptime = round(_time_mod.monotonic() - _start_time, 1)

    overall = "ok" if db_status == "ok" else "degraded"

    return {
        "status": overall,
        "providers": providers_status,
        "db": db_status,
        "uptime_seconds": uptime,
    }


@app.get("/api/v1/admin/check-update")
async def check_update(authorization: str = Header(None)):
    """Check if a newer version is available on GitHub."""
    await require_master(authorization)
    import subprocess
    try:
        # Get latest release tag from GitHub
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.github.com/repos/Dandona100/SafeEyes/commits/main",
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    remote_sha = data.get("sha", "")[:7]
                    remote_msg = data.get("commit", {}).get("message", "").split("\n")[0]
                    remote_date = data.get("commit", {}).get("committer", {}).get("date", "")
                else:
                    return {"status": "error", "message": f"GitHub API returned {resp.status}"}

        # Get local commit SHA
        local_sha = ""
        try:
            import subprocess as _sp
            result = _sp.run(["git", "rev-parse", "--short=7", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=os.path.dirname(os.path.dirname(__file__)))
            local_sha = result.stdout.strip()
        except Exception:
            pass

        return {
            "status": "ok",
            "local_version": VERSION,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
            "remote_message": remote_msg,
            "remote_date": remote_date,
            "update_available": local_sha != remote_sha and bool(local_sha),
            "install_command": "cd SafeEyes && git pull && docker compose up -d --build",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/server-info")
async def server_info(authorization: str = Header(None)):
    """Protected endpoint — returns server user, IP, port for SSH instructions."""
    await require_token(authorization)
    import subprocess
    user = os.environ.get("USER", os.environ.get("LOGNAME", subprocess.getoutput("whoami").strip() or "user"))
    ip = subprocess.getoutput("curl -s https://api.ipify.org 2>/dev/null || echo YOUR_SERVER_IP").strip()
    port = int(os.environ.get("SCAN_PORT", 1985))
    return {"user": user, "ip": ip, "port": port}
