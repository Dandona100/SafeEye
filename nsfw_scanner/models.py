"""Pydantic models for the NSFW Scanner API."""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class ProviderResult(BaseModel):
    provider: str
    is_nsfw: bool
    confidence: float = 0.0
    labels: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    error: bool = False
    skipped: bool = False


class AggregatedResult(BaseModel):
    is_nsfw: bool
    borderline: bool = False
    confidence: float = 0.0
    labels: list[str] = Field(default_factory=list)
    providers_agree: int = 0
    providers_total: int = 0
    provider_results: list[ProviderResult] = Field(default_factory=list)
    scan_id: str = ""
    scan_duration_ms: float = 0.0
    phash: Optional[str] = None


class ScanResponse(BaseModel):
    scan_id: str
    result: AggregatedResult
    timestamp: str


class TokenCreate(BaseModel):
    name: str
    expires_in_days: Optional[int] = None


class TokenInfo(BaseModel):
    name: str
    created_at: str
    expires_at: Optional[str] = None
    enabled: bool = True
    last_used: Optional[str] = None
    scan_count: int = 0


class TokenCreated(BaseModel):
    name: str
    token: str  # Raw token, shown only once


class FeedbackRequest(BaseModel):
    actual_nsfw: bool
    notes: Optional[str] = None


class StatsOverview(BaseModel):
    total_scans: int = 0
    nsfw_detected: int = 0
    nsfw_rate: float = 0.0
    borderline_count: int = 0
    scans_today: int = 0
    avg_scan_ms: float = 0.0
    providers_active: list[str] = Field(default_factory=list)
    blocklist_size: int = 0


class ProviderStats(BaseModel):
    provider: str
    total_scans: int = 0
    nsfw_flagged: int = 0
    avg_latency_ms: float = 0.0
    error_count: int = 0
    skip_count: int = 0
    accuracy: Optional[float] = None  # None if no feedback data


class HistoryItem(BaseModel):
    scan_id: str
    timestamp: str
    file_type: Optional[str] = None
    is_nsfw: bool
    borderline: bool = False
    confidence: float = 0.0
    labels: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    providers_agree: int = 0
    providers_total: int = 0


# ========== Jobs & Batch ==========

class JobResponse(BaseModel):
    job_id: str
    status: str = "pending"  # pending, processing, completed, failed
    result: Optional[AggregatedResult] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class BatchRequest(BaseModel):
    urls: list[str]
    webhook_url: Optional[str] = None


class BatchResponse(BaseModel):
    batch_id: str
    total: int
    status: str = "processing"
    completed: int = 0
    failed: int = 0
    pending: int = 0
    results: list[JobResponse] = Field(default_factory=list)
