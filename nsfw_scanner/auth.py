"""Token authentication for the scanner API."""
import os
import hashlib
import secrets
from datetime import datetime
from nsfw_scanner import db


def generate_token() -> tuple[str, str]:
    """Generate (raw_token, token_hash) pair."""
    raw = secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_master(token: str) -> bool:
    """Check if token matches SCAN_API_MASTER_TOKEN env var."""
    master = os.environ.get("SCAN_API_MASTER_TOKEN", "")
    return bool(master) and secrets.compare_digest(token, master)


async def verify_api_token(raw_token: str) -> dict | None:
    """Verify an API token. Returns token info or None."""
    token_hash = hash_token(raw_token)
    token_data = await db.get_token(token_hash)

    if not token_data:
        return None
    if not token_data["enabled"]:
        return None

    # Check expiry
    expires = token_data.get("expires_at")
    if expires and datetime.fromisoformat(expires) < datetime.utcnow():
        return None

    return token_data
