"""Statistics query functions for the dashboard and API."""
import json
from datetime import datetime, timedelta
from nsfw_scanner.db import get_db
from nsfw_scanner.models import StatsOverview, ProviderStats, HistoryItem
from nsfw_scanner.scanner import get_active_providers


async def get_overview(requesting_token: str = None) -> StatsOverview:
    db = await get_db()
    try:
        # Build optional token filter
        token_filter = ""
        token_params: tuple = ()
        if requesting_token:
            token_filter = " WHERE requesting_token=?"
            token_params = (requesting_token,)

        row = await db.execute_fetchall(f"SELECT COUNT(*) as c FROM scan_history{token_filter}", token_params)
        total = row[0][0] if row else 0

        nsfw_clause = f"{'WHERE' if not requesting_token else 'WHERE requesting_token=? AND'} is_nsfw=1"
        nsfw_params = token_params
        row = await db.execute_fetchall(f"SELECT COUNT(*) as c FROM scan_history {nsfw_clause}", nsfw_params)
        nsfw = row[0][0] if row else 0

        borderline_clause = f"{'WHERE' if not requesting_token else 'WHERE requesting_token=? AND'} borderline=1"
        row = await db.execute_fetchall(f"SELECT COUNT(*) as c FROM scan_history {borderline_clause}", token_params)
        borderline = row[0][0] if row else 0

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if requesting_token:
            row = await db.execute_fetchall(
                "SELECT COUNT(*) FROM scan_history WHERE requesting_token=? AND timestamp >= ?",
                (requesting_token, today),
            )
        else:
            row = await db.execute_fetchall(
                "SELECT COUNT(*) FROM scan_history WHERE timestamp >= ?", (today,)
            )
        scans_today = row[0][0] if row else 0

        row = await db.execute_fetchall(f"SELECT AVG(total_duration_ms) FROM scan_history{token_filter}", token_params)
        avg_ms = row[0][0] if row and row[0][0] else 0

        # Load blocklist size
        blocklist_size = 0
        try:
            import os
            for p in ["/app/services/nsfw_domains.txt", "/app/data/nsfw_domains.txt"]:
                if os.path.exists(p):
                    with open(p) as f:
                        blocklist_size = sum(1 for l in f if l.strip() and not l.startswith("#"))
                    break
        except Exception:
            pass

        return StatsOverview(
            total_scans=total,
            nsfw_detected=nsfw,
            nsfw_rate=round(nsfw / total * 100, 1) if total > 0 else 0,
            borderline_count=borderline,
            scans_today=scans_today,
            avg_scan_ms=round(avg_ms, 1),
            providers_active=get_active_providers(),
            blocklist_size=blocklist_size,
        )
    finally:
        await db.close()


async def get_provider_stats() -> list[ProviderStats]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("""
            SELECT
                provider,
                COUNT(*) as total,
                SUM(is_nsfw) as flagged,
                AVG(latency_ms) as avg_lat,
                SUM(error) as errors,
                SUM(skipped) as skips
            FROM provider_results
            GROUP BY provider
        """)

        results = []
        for r in rows:
            r = dict(r)
            # Calculate accuracy if feedback exists
            acc_rows = await db.execute_fetchall("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN pr.is_nsfw = af.actual_nsfw THEN 1 ELSE 0 END) as correct
                FROM provider_results pr
                JOIN accuracy_feedback af ON pr.scan_id = af.scan_id
                WHERE pr.provider = ?
            """, (r["provider"],))

            accuracy = None
            if acc_rows and acc_rows[0][0] > 0:
                accuracy = round(acc_rows[0][1] / acc_rows[0][0] * 100, 1)

            results.append(ProviderStats(
                provider=r["provider"],
                total_scans=r["total"],
                nsfw_flagged=r["flagged"] or 0,
                avg_latency_ms=round(r["avg_lat"] or 0, 1),
                error_count=r["errors"] or 0,
                skip_count=r["skips"] or 0,
                accuracy=accuracy,
            ))
        return results
    finally:
        await db.close()


async def get_history(limit: int = 50, offset: int = 0, nsfw_only: bool = False,
                      requesting_token: str = None) -> list[HistoryItem]:
    db = await get_db()
    try:
        conditions = []
        params: list = []
        if nsfw_only:
            conditions.append("is_nsfw=1")
        if requesting_token:
            conditions.append("requesting_token=?")
            params.append(requesting_token)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        rows = await db.execute_fetchall(
            f"SELECT * FROM scan_history {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [
            HistoryItem(
                scan_id=dict(r)["id"],
                timestamp=dict(r)["timestamp"],
                file_type=dict(r).get("file_type"),
                is_nsfw=bool(dict(r)["is_nsfw"]),
                borderline=bool(dict(r).get("borderline", 0)),
                confidence=dict(r).get("confidence", 0),
                labels=json.loads(dict(r).get("labels", "[]")),
                duration_ms=dict(r).get("total_duration_ms", 0),
                providers_agree=dict(r).get("providers_agree", 0),
                providers_total=dict(r).get("providers_total", 0),
            )
            for r in rows
        ]
    finally:
        await db.close()
