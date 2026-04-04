"""Live stream monitoring — periodically extracts frames via ffmpeg and scans them."""
import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from nsfw_scanner.scanner import scan_file

logger = logging.getLogger(__name__)

TEMP_DIR = os.environ.get("SCAN_TEMP_DIR", "/tmp/nsfw_scans")


@dataclass
class StreamAlert:
    """A single NSFW detection event on a stream."""
    timestamp: str
    confidence: float
    labels: list[str]
    scan_id: str


@dataclass
class StreamMonitorState:
    """Tracks the runtime state of one monitored stream."""
    stream_url: str
    interval_seconds: int
    webhook_url: Optional[str]
    started_at: str
    frames_scanned: int = 0
    nsfw_detections: int = 0
    last_scan_at: Optional[str] = None
    last_error: Optional[str] = None
    alerts: list[StreamAlert] = field(default_factory=list)
    task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        return {
            "stream_url": self.stream_url,
            "interval_seconds": self.interval_seconds,
            "webhook_url": self.webhook_url,
            "started_at": self.started_at,
            "frames_scanned": self.frames_scanned,
            "nsfw_detections": self.nsfw_detections,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
            "recent_alerts": [
                {
                    "timestamp": a.timestamp,
                    "confidence": a.confidence,
                    "labels": a.labels,
                    "scan_id": a.scan_id,
                }
                for a in self.alerts[-10:]  # Keep last 10 alerts in status
            ],
        }


# In-memory registry: stream_url -> StreamMonitorState
_active_monitors: dict[str, StreamMonitorState] = {}


async def _capture_frame(stream_url: str, output_path: str, timeout: int = 15) -> bool:
    """Use ffmpeg to grab a single frame from a live stream.

    Returns True if a frame was successfully captured.
    """
    cmd = [
        "ffmpeg",
        "-y",                   # overwrite output
        "-loglevel", "error",
        "-i", stream_url,
        "-vframes", "1",        # grab exactly one frame
        "-q:v", "2",            # reasonable JPEG quality
        output_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            logger.warning("ffmpeg frame capture failed (rc=%d): %s", proc.returncode, err_msg[:200])
            return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except asyncio.TimeoutError:
        logger.warning("ffmpeg frame capture timed out for %s", stream_url)
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except ProcessLookupError:
            pass
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found — install ffmpeg to use stream monitoring")
        return False
    except Exception as exc:
        logger.error("Unexpected error during frame capture: %s", exc)
        return False


async def _send_webhook(webhook_url: str, payload: dict) -> None:
    """POST an alert payload to the configured webhook URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    logger.warning("Webhook POST to %s returned HTTP %d", webhook_url, resp.status)
                else:
                    logger.info("Webhook alert sent to %s (HTTP %d)", webhook_url, resp.status)
    except Exception as exc:
        logger.error("Webhook delivery failed: %s", exc)


async def monitor_stream(
    stream_url: str,
    interval_seconds: int = 10,
    webhook_url: Optional[str] = None,
) -> None:
    """Core monitoring loop — runs until the task is cancelled.

    Captures a frame every *interval_seconds*, scans it, and fires a
    webhook + log entry when NSFW content is detected.
    """
    state = _active_monitors.get(stream_url)
    if state is None:
        logger.error("monitor_stream called but no state registered for %s", stream_url)
        return

    os.makedirs(TEMP_DIR, exist_ok=True)
    consecutive_errors = 0
    max_consecutive_errors = 10

    logger.info("Stream monitor started: %s (interval=%ds)", stream_url, interval_seconds)

    try:
        while True:
            frame_path = tempfile.NamedTemporaryFile(
                dir=TEMP_DIR, suffix=".jpg", prefix="stream_", delete=False,
            ).name

            try:
                captured = await _capture_frame(stream_url, frame_path)

                if not captured:
                    consecutive_errors += 1
                    state.last_error = f"Frame capture failed ({consecutive_errors} consecutive)"
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(
                            "Stream %s: %d consecutive capture failures — stopping monitor",
                            stream_url, consecutive_errors,
                        )
                        state.last_error = f"Stopped: {consecutive_errors} consecutive capture failures"
                        break
                    await asyncio.sleep(interval_seconds)
                    continue

                consecutive_errors = 0
                now_iso = datetime.utcnow().isoformat()

                result = await scan_file(frame_path)
                state.frames_scanned += 1
                state.last_scan_at = now_iso

                if result.is_nsfw:
                    state.nsfw_detections += 1
                    alert = StreamAlert(
                        timestamp=now_iso,
                        confidence=result.confidence,
                        labels=result.labels,
                        scan_id=result.scan_id,
                    )
                    state.alerts.append(alert)

                    logger.warning(
                        "NSFW detected on stream %s — confidence=%.2f labels=%s scan_id=%s",
                        stream_url, result.confidence, result.labels, result.scan_id,
                    )

                    if webhook_url:
                        await _send_webhook(webhook_url, {
                            "event": "nsfw_detected",
                            "stream_url": stream_url,
                            "timestamp": now_iso,
                            "confidence": result.confidence,
                            "labels": result.labels,
                            "scan_id": result.scan_id,
                            "providers_agree": result.providers_agree,
                            "providers_total": result.providers_total,
                        })

                state.last_error = None

            finally:
                if os.path.exists(frame_path):
                    os.unlink(frame_path)

            await asyncio.sleep(interval_seconds)

    except asyncio.CancelledError:
        logger.info("Stream monitor cancelled: %s", stream_url)
        raise
    except Exception as exc:
        logger.error("Stream monitor crashed for %s: %s", stream_url, exc, exc_info=True)
        state.last_error = f"Crashed: {exc}"
    finally:
        # Clean up from registry only if we are the current task
        current = _active_monitors.get(stream_url)
        if current is not None and current.task is asyncio.current_task():
            _active_monitors.pop(stream_url, None)
            logger.info("Stream monitor removed from registry: %s", stream_url)


def start_monitor(
    stream_url: str,
    interval_seconds: int = 10,
    webhook_url: Optional[str] = None,
) -> StreamMonitorState:
    """Create and register a new stream monitor task.

    Raises ValueError if the stream is already being monitored.
    """
    if stream_url in _active_monitors:
        existing = _active_monitors[stream_url]
        if existing.task and not existing.task.done():
            raise ValueError(f"Stream is already being monitored: {stream_url}")
        # Previous task finished — allow restart
        _active_monitors.pop(stream_url, None)

    state = StreamMonitorState(
        stream_url=stream_url,
        interval_seconds=interval_seconds,
        webhook_url=webhook_url,
        started_at=datetime.utcnow().isoformat(),
    )

    task = asyncio.create_task(
        monitor_stream(stream_url, interval_seconds, webhook_url),
        name=f"stream-monitor-{stream_url[:60]}",
    )
    state.task = task
    _active_monitors[stream_url] = state
    return state


def stop_monitor(stream_url: str) -> bool:
    """Cancel the monitor task for a given stream URL.

    Returns True if a monitor was found and cancelled, False otherwise.
    """
    state = _active_monitors.get(stream_url)
    if state is None:
        return False
    if state.task and not state.task.done():
        state.task.cancel()
    _active_monitors.pop(stream_url, None)
    return True


def get_all_monitors() -> dict[str, dict]:
    """Return status dicts for all active monitors."""
    result = {}
    for url, state in list(_active_monitors.items()):
        # Prune finished tasks
        if state.task and state.task.done():
            _active_monitors.pop(url, None)
            continue
        result[url] = state.to_dict()
    return result
