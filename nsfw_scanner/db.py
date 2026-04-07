"""SQLite database for scan history, tokens, and accuracy feedback."""
import os
import json
import aiosqlite
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SCAN_DB_PATH", "/app/data/scan_stats.db")
# Fallback for local dev
if not os.path.exists(os.path.dirname(DB_PATH)):
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scan_stats.db")


_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return singleton DB connection. PRAGMAs run once on first connect."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        # Override close to be a no-op coroutine (singleton stays open)
        _db._real_close = _db.close
        async def _noop(): pass
        _db.close = _noop
    return _db


async def close_db():
    """Actually close the DB connection (call on shutdown)."""
    global _db
    if _db:
        await _db._real_close()
        _db = None


async def init_db():
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'api',
                file_type TEXT,
                is_nsfw INTEGER NOT NULL DEFAULT 0,
                borderline INTEGER NOT NULL DEFAULT 0,
                confidence REAL DEFAULT 0,
                labels TEXT DEFAULT '[]',
                providers_agree INTEGER DEFAULT 0,
                providers_total INTEGER DEFAULT 0,
                total_duration_ms REAL DEFAULT 0,
                requesting_token TEXT,
                phash TEXT
            );

            CREATE TABLE IF NOT EXISTS provider_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL REFERENCES scan_history(id),
                provider TEXT NOT NULL,
                is_nsfw INTEGER NOT NULL DEFAULT 0,
                confidence REAL DEFAULT 0,
                labels TEXT DEFAULT '[]',
                latency_ms REAL DEFAULT 0,
                error INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS accuracy_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL REFERENCES scan_history(id),
                actual_nsfw INTEGER NOT NULL,
                feedback_time TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS api_tokens (
                token_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                enabled INTEGER DEFAULT 1,
                last_used TEXT,
                scan_count INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS provider_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                batch_id TEXT,
                status TEXT DEFAULT 'pending',
                type TEXT NOT NULL,
                input_url TEXT,
                file_path TEXT,
                webhook_url TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                requesting_token TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

            CREATE TABLE IF NOT EXISTS community_reports (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                device_uuid TEXT,
                vote_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                created_at TEXT NOT NULL,
                github_url TEXT
            );

            CREATE TABLE IF NOT EXISTS community_votes (
                report_id TEXT NOT NULL,
                device_uuid TEXT NOT NULL,
                PRIMARY KEY (report_id, device_uuid)
            );

            CREATE INDEX IF NOT EXISTS idx_scan_phash ON scan_history(phash);
            CREATE INDEX IF NOT EXISTS idx_scan_timestamp ON scan_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_scan_nsfw ON scan_history(is_nsfw);
            CREATE INDEX IF NOT EXISTS idx_provider_scan ON provider_results(scan_id);
            CREATE INDEX IF NOT EXISTS idx_provider_name ON provider_results(provider);
            CREATE TABLE IF NOT EXISTS webhook_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                webhook_url TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 5,
                next_retry TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_queue(status);
            CREATE INDEX IF NOT EXISTS idx_webhook_next_retry ON webhook_queue(next_retry);
            CREATE INDEX IF NOT EXISTS idx_community_type ON community_reports(type);
            CREATE INDEX IF NOT EXISTS idx_community_votes ON community_reports(vote_count);
        """)
        await db.commit()

        # Migrate: add phash column if missing (existing databases)
        try:
            await db.execute("ALTER TABLE scan_history ADD COLUMN phash TEXT")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_scan_phash ON scan_history(phash)")
            await db.commit()
        except Exception:
            pass  # Column already exists

        # Migrate: add priority column to api_tokens if missing
        try:
            await db.execute("ALTER TABLE api_tokens ADD COLUMN priority INTEGER DEFAULT 1")
            await db.commit()
        except Exception:
            pass  # Column already exists
    finally:
        await db.close()


async def insert_scan(scan_id: str, file_type: str, result: dict, token_name: str = None):
    """Insert a scan result and per-provider results."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO scan_history
               (id, timestamp, source, file_type, is_nsfw, borderline, confidence,
                labels, providers_agree, providers_total, total_duration_ms, requesting_token, phash)
               VALUES (?, ?, 'api', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan_id,
                datetime.utcnow().isoformat(),
                file_type,
                int(result["is_nsfw"]),
                int(result.get("borderline", False)),
                result["confidence"],
                json.dumps(result["labels"]),
                result["providers_agree"],
                result["providers_total"],
                result["scan_duration_ms"],
                token_name,
                result.get("phash"),
            ),
        )
        for pr in result.get("provider_results", []):
            await db.execute(
                """INSERT INTO provider_results
                   (scan_id, provider, is_nsfw, confidence, labels, latency_ms, error, skipped)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id,
                    pr["provider"],
                    int(pr["is_nsfw"]),
                    pr["confidence"],
                    json.dumps(pr["labels"]),
                    pr["latency_ms"],
                    int(pr.get("error", False)),
                    int(pr.get("skipped", False)),
                ),
            )
        await db.commit()
    finally:
        await db.close()


async def get_scan(scan_id: str, requesting_token: str = None) -> dict | None:
    db = await get_db()
    try:
        if requesting_token:
            row = await db.execute_fetchall(
                "SELECT * FROM scan_history WHERE id=? AND requesting_token=?",
                (scan_id, requesting_token),
            )
        else:
            row = await db.execute_fetchall("SELECT * FROM scan_history WHERE id=?", (scan_id,))
        if not row:
            return None
        scan = dict(row[0])
        scan["labels"] = json.loads(scan["labels"])
        providers = await db.execute_fetchall(
            "SELECT * FROM provider_results WHERE scan_id=?", (scan_id,)
        )
        scan["provider_results"] = [
            {**dict(p), "labels": json.loads(dict(p)["labels"])} for p in providers
        ]
        return scan
    finally:
        await db.close()


from nsfw_scanner.scanner import hamming_distance as _hamming_distance


async def find_similar_by_phash(phash: str, threshold: int = 10, limit: int = 50) -> list[dict]:
    """Find scans with perceptual hashes within the given Hamming distance."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, timestamp, file_type, is_nsfw, confidence, labels, phash "
            "FROM scan_history WHERE phash IS NOT NULL"
        )
        results = []
        for row in rows:
            row_dict = dict(row)
            distance = _hamming_distance(phash, row_dict["phash"])
            if distance <= threshold:
                row_dict["labels"] = json.loads(row_dict["labels"])
                row_dict["hamming_distance"] = distance
                results.append(row_dict)
        results.sort(key=lambda r: r["hamming_distance"])
        return results[:limit]
    finally:
        await db.close()


async def get_all_phashes() -> list[dict]:
    """Return all scans that have a non-null pHash (id + phash only)."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, phash FROM scan_history WHERE phash IS NOT NULL"
        )
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def search_scans_by_labels(query: str, limit: int = 50) -> list[dict]:
    """Search scan history by keyword matching against stored labels.

    Each token in *query* is matched case-insensitively against the JSON-encoded
    labels column.  A row matches if **all** tokens appear somewhere in its labels.
    Results are ordered by confidence descending.
    """
    tokens = query.lower().split()
    if not tokens:
        return []

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, timestamp, file_type, is_nsfw, confidence, labels, phash "
            "FROM scan_history WHERE labels IS NOT NULL AND labels != '[]' "
            "ORDER BY confidence DESC"
        )
        results = []
        for row in rows:
            row_dict = dict(row)
            labels_raw = row_dict["labels"]
            labels_lower = labels_raw.lower()
            if all(tok in labels_lower for tok in tokens):
                row_dict["labels"] = json.loads(labels_raw)
                results.append(row_dict)
            if len(results) >= limit:
                break
        return results
    finally:
        await db.close()


async def insert_feedback(scan_id: str, actual_nsfw: bool, notes: str = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO accuracy_feedback (scan_id, actual_nsfw, feedback_time, notes) VALUES (?, ?, ?, ?)",
            (scan_id, int(actual_nsfw), datetime.utcnow().isoformat(), notes),
        )
        await db.commit()
    finally:
        await db.close()


# Token operations

async def insert_token(token_hash: str, name: str, expires_at: str = None, priority: int = 1):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO api_tokens (token_hash, name, created_at, expires_at, priority) VALUES (?, ?, ?, ?, ?)",
            (token_hash, name, datetime.utcnow().isoformat(), expires_at, priority),
        )
        await db.commit()
    finally:
        await db.close()


async def get_token(token_hash: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM api_tokens WHERE token_hash=?", (token_hash,))
        if not rows:
            return None
        return dict(rows[0])
    finally:
        await db.close()


async def list_tokens() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT name, created_at, expires_at, enabled, last_used, scan_count FROM api_tokens ORDER BY created_at DESC")
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def delete_token(name: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM api_tokens WHERE name=?", (name,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def save_provider_config(key: str, value: str):
    """Save a provider config key-value pair."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO provider_config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_provider_config(keys: list[str]):
    """Delete provider config keys."""
    db = await get_db()
    try:
        for key in keys:
            await db.execute("DELETE FROM provider_config WHERE key=?", (key,))
        await db.commit()
    finally:
        await db.close()


async def load_all_provider_config() -> dict[str, str]:
    """Load all provider config into a dict."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT key, value FROM provider_config")
        return {row[0]: row[1] for row in rows}
    finally:
        await db.close()


async def bump_token_usage(token_hash: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE api_tokens SET scan_count=scan_count+1, last_used=? WHERE token_hash=?",
            (datetime.utcnow().isoformat(), token_hash),
        )
        await db.commit()
    finally:
        await db.close()


# Metadata export for client-side pre-scan

async def export_hash_metadata() -> list[dict]:
    """Export phash→result metadata for client-side matching.
    Returns compact records: {p: phash, n: is_nsfw, c: confidence, l: labels}."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT phash, is_nsfw, confidence, labels FROM scan_history "
            "WHERE phash IS NOT NULL ORDER BY timestamp DESC LIMIT 10000"
        )
        return [
            {"p": (r := dict(row))["phash"], "n": r["is_nsfw"],
             "c": round(r["confidence"], 2), "l": json.loads(r["labels"]) if r["labels"] else []}
            for row in rows
        ]
    finally:
        await db.close()


async def import_hash_metadata(records: list[dict], source: str):
    """Import hash metadata from another SafeEyes server.
    Merges into scan_history with source='shared:<origin>'."""
    db = await get_db()
    try:
        for rec in records:
            phash = rec.get("p", "")
            if not phash:
                continue
            existing = await db.execute_fetchall(
                "SELECT 1 FROM scan_history WHERE phash=?", (phash,)
            )
            if existing:
                continue
            scan_id = f"shared_{phash[:12]}"
            await db.execute(
                "INSERT OR IGNORE INTO scan_history "
                "(id, timestamp, source, is_nsfw, confidence, labels, phash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    datetime.utcnow().isoformat(),
                    f"shared:{source}",
                    rec.get("n", 0),
                    rec.get("c", 0),
                    json.dumps(rec.get("l", [])),
                    phash,
                ),
            )
        await db.commit()
    finally:
        await db.close()


# Community operations

async def insert_community_report(report_id: str, report_type: str, title: str, description: str, device_uuid: str) -> dict:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO community_reports (id, type, title, description, device_uuid, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (report_id, report_type, title, description, device_uuid, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return {"id": report_id, "type": report_type, "title": title}
    finally:
        await db.close()


async def list_community_reports(report_type: str = None, sort: str = "votes", limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        where = "WHERE type=?" if report_type else ""
        order = "vote_count DESC" if sort == "votes" else "created_at DESC"
        params = (report_type,) if report_type else ()
        rows = await db.execute_fetchall(
            f"SELECT * FROM community_reports {where} ORDER BY {order} LIMIT ?",
            (*params, limit),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_community_report(report_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM community_reports WHERE id=?", (report_id,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def vote_community_report(report_id: str, device_uuid: str) -> dict:
    db = await get_db()
    try:
        # Check if already voted
        existing = await db.execute_fetchall(
            "SELECT 1 FROM community_votes WHERE report_id=? AND device_uuid=?",
            (report_id, device_uuid),
        )
        if existing:
            return {"status": "already_voted"}

        await db.execute(
            "INSERT INTO community_votes (report_id, device_uuid) VALUES (?, ?)",
            (report_id, device_uuid),
        )
        await db.execute(
            "UPDATE community_reports SET vote_count=vote_count+1 WHERE id=?",
            (report_id,),
        )
        await db.commit()

        row = await db.execute_fetchall("SELECT vote_count FROM community_reports WHERE id=?", (report_id,))
        return {"status": "voted", "vote_count": row[0][0] if row else 0}
    finally:
        await db.close()


# Job operations

async def create_job(job_id: str, job_type: str, input_url: str = None, file_path: str = None,
                     webhook_url: str = None, batch_id: str = None, token_name: str = None):
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO jobs (id, batch_id, status, type, input_url, file_path, webhook_url, created_at, requesting_token)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (job_id, batch_id, job_type, input_url, file_path, webhook_url, datetime.utcnow().isoformat(), token_name),
        )
        await db.commit()
    finally:
        await db.close()


async def update_job(job_id: str, status: str, result_json: str = None, error: str = None):
    db = await get_db()
    try:
        completed = datetime.utcnow().isoformat() if status in ("completed", "failed") else None
        await db.execute(
            "UPDATE jobs SET status=?, result=?, error=?, completed_at=? WHERE id=?",
            (status, result_json, error, completed, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_job(job_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM jobs WHERE id=?", (job_id,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def get_batch_jobs(batch_id: str) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at", (batch_id,))
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ========== Webhook Queue Operations ==========

async def queue_webhook(job_id: str, webhook_url: str, payload: str):
    """Add a webhook delivery to the persistent retry queue."""
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO webhook_queue (job_id, webhook_url, payload, attempts, max_attempts, next_retry, status, created_at)
               VALUES (?, ?, ?, 0, 5, ?, 'pending', ?)""",
            (job_id, webhook_url, payload, now, now),
        )
        await db.commit()
    finally:
        await db.close()


async def get_pending_webhooks() -> list[dict]:
    """Get all pending webhooks whose next_retry time has passed."""
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        rows = await db.execute_fetchall(
            "SELECT * FROM webhook_queue WHERE status='pending' AND next_retry <= ? ORDER BY next_retry",
            (now,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_webhook_status(webhook_id: int, status: str, attempts: int = None, next_retry: str = None):
    """Update a webhook queue entry's status, attempt count, and next retry time."""
    db = await get_db()
    try:
        sets, params = ["status=?"], [status]
        if attempts is not None: sets.append("attempts=?"); params.append(attempts)
        if next_retry is not None: sets.append("next_retry=?"); params.append(next_retry)
        params.append(webhook_id)
        await db.execute(f"UPDATE webhook_queue SET {', '.join(sets)} WHERE id=?", tuple(params))
        await db.commit()
    finally:
        await db.close()


# ========== Token Rotation ==========

async def rotate_token(name: str) -> tuple[str, str] | None:
    """Rotate a token: create a new one with the same name, set old one to expire in 24h.

    Returns (new_raw_token, new_token_hash) or None if token name not found.
    """
    import secrets
    import hashlib

    db = await get_db()
    try:
        # Find existing token by name
        rows = await db.execute_fetchall("SELECT * FROM api_tokens WHERE name=?", (name,))
        if not rows:
            return None

        old_token = dict(rows[0])
        old_hash = old_token["token_hash"]

        # Set old token to expire in 24 hours
        grace_expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
        await db.execute(
            "UPDATE api_tokens SET expires_at=? WHERE token_hash=?",
            (grace_expires, old_hash),
        )

        # Temporarily rename old token so UNIQUE constraint on name allows the new insert
        old_name_suffix = f"_rotated_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        await db.execute(
            "UPDATE api_tokens SET name=? WHERE token_hash=?",
            (name + old_name_suffix, old_hash),
        )

        # Generate new token
        raw = secrets.token_urlsafe(32)
        hashed = hashlib.sha256(raw.encode()).hexdigest()

        await db.execute(
            "INSERT INTO api_tokens (token_hash, name, created_at, expires_at, enabled) VALUES (?, ?, ?, ?, 1)",
            (hashed, name, datetime.utcnow().isoformat(), None),
        )
        await db.commit()
        return raw, hashed
    finally:
        await db.close()
