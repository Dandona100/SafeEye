"""Entry point: start uvicorn on configured port."""
import os
import socket
import subprocess
import uvicorn

DEFAULT_PORT = 1985


def find_available_port(preferred: int) -> int:
    """Find an available port, starting from preferred."""
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    return preferred


def get_public_ip():
    try:
        return subprocess.getoutput("curl -s https://api.ipify.org 2>/dev/null").strip() or "unknown"
    except Exception:
        return "unknown"


def get_user():
    return os.environ.get("USER", os.environ.get("LOGNAME", subprocess.getoutput("whoami").strip() or "user"))


port = int(os.environ.get("SCAN_PORT", str(DEFAULT_PORT)))
port = find_available_port(port)
ip = get_public_ip()
user = get_user()
master = os.environ.get("SCAN_API_MASTER_TOKEN", "")
if not master:
    # Auto-generate master token on first run
    import secrets as _secrets
    master = _secrets.token_urlsafe(32)
    os.environ["SCAN_API_MASTER_TOKEN"] = master
    # Try to persist to .env file
    _env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    try:
        with open(_env_path, "a") as _f:
            _f.write(f"\nSCAN_API_MASTER_TOKEN={master}\n")
    except OSError:
        pass  # read-only FS, user will see token in logs

log_level = os.environ.get("LOG_LEVEL", "info").lower()

# System checks
import shutil
import multiprocessing

warnings = []
cpu_count = multiprocessing.cpu_count()
try:
    import psutil
    ram_gb = psutil.virtual_memory().total / (1024**3)
    ram_avail = psutil.virtual_memory().available / (1024**3)
except ImportError:
    ram_gb = 0
    ram_avail = 0

disk_free = shutil.disk_usage("/").free / (1024**3)

if cpu_count < 2:
    warnings.append(f"  ⚠️  CPU: {cpu_count} core(s) — 2+ recommended for parallel scanning")
if ram_gb > 0 and ram_gb < 0.5:
    warnings.append(f"  ⚠️  RAM: {ram_gb:.1f}GB total — 512MB+ recommended")
if disk_free < 1:
    warnings.append(f"  ⚠️  Disk: {disk_free:.1f}GB free — 1GB+ recommended")

try:
    import cv2
    has_opencv = True
except ImportError:
    has_opencv = False
    warnings.append("  ⚠️  OpenCV not installed — video scanning disabled")

try:
    from nudenet import NudeDetector
    has_nudenet = True
except ImportError:
    has_nudenet = False
    warnings.append("  ⚠️  NudeNet not installed — local scanning disabled")

print()
print("=" * 60)
print("  🛡️  SafeEyes — Content Safety Scanner")
print("=" * 60)
print()
print(f"  ✅ Running on port {port}")
print(f"  🌐 Dashboard:  http://localhost:{port}/dashboard")
print(f"  📡 Health:     http://localhost:{port}/health")
print()
print(f"  💻 System: {cpu_count} CPU cores | {ram_gb:.1f}GB RAM | {disk_free:.1f}GB disk free")
print(f"  📦 NudeNet: {'✅' if has_nudenet else '❌'} | OpenCV: {'✅' if has_opencv else '❌'}")
if warnings:
    print()
    for w in warnings:
        print(w)
print()
print(f"  🔑 Master Token: {master[:8]}... (saved to .env)")
print()
print("  📋 No domain? Use SSH tunnel from your computer:")
print(f"     ssh -L {port}:localhost:{port} {user}@{ip}")
print(f"     Then open: http://localhost:{port}/dashboard")
print()
print("  📚 Full docs:  https://github.com/Dandona100/SafeEyes")
print("  💡 Suggest:    https://github.com/Dandona100/SafeEyes/issues")
print("  🤝 Contribute: https://github.com/Dandona100/SafeEyes/pulls")
print("  📧 Contact:    https://t.me/DVS20")
print()
print(f"  🐛 Debug:      LOG_LEVEL={log_level} (set in .env)")
print(f"               docker compose logs -f nsfw_scanner")
print()

# Check for updates on startup
try:
    import urllib.request, json as _json
    req = urllib.request.Request("https://api.github.com/repos/Dandona100/SafeEyes/commits/main",
                                 headers={"Accept": "application/vnd.github.v3+json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = _json.loads(resp.read())
        sha = data.get("sha", "")[:7]
        msg = data.get("commit", {}).get("message", "").split("\n")[0][:50]
        print(f"  🔄 Latest on GitHub: {sha} — {msg}")
except Exception:
    pass

print()
print("=" * 60)
print()

uvicorn.run("nsfw_scanner.app:app", host="0.0.0.0", port=port, log_level=log_level)
