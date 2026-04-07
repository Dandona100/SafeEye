"""Community endpoints — public bug reports, feature requests, voting."""
import uuid
from fastapi import APIRouter, HTTPException, Query
from nsfw_scanner import db as database

router = APIRouter(prefix="/api/v1/community", tags=["community"])


@router.get("")
async def list_community(type: str = Query(None), sort: str = Query("votes"), limit: int = Query(50)):
    return await database.list_community_reports(type, sort, limit)


@router.post("")
async def create_community_report(body: dict):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "Title required")
    return await database.insert_community_report(
        uuid.uuid4().hex[:12], body.get("type", "feature"),
        title, body.get("description", ""), body.get("device_uuid", "anonymous"),
    )


@router.get("/{report_id}")
async def get_community_report(report_id: str):
    report = await database.get_community_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@router.post("/{report_id}/vote")
async def vote_community(report_id: str, body: dict):
    device_uuid = body.get("device_uuid", "")
    if not device_uuid:
        raise HTTPException(400, "device_uuid required")
    report = await database.get_community_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return await database.vote_community_report(report_id, device_uuid)
