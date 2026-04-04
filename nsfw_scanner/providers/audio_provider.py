"""Audio metadata provider using ffprobe.

Extracts audio metadata (duration, codec, bitrate) and flags files
that contain audio content. This is a foundation for future speech-to-text
toxicity scanning.
"""
import asyncio
import json
import os
import shutil
import time
import logging

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".webm"}


async def _get_audio_info(file_path: str) -> dict:
    """Run ffprobe and return audio stream metadata."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        file_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace").strip()}

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        return {"error": "invalid_ffprobe_json"}

    # Find the first audio stream
    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break

    if audio_stream is None:
        return {"has_audio": False}

    fmt = data.get("format", {})
    duration_str = fmt.get("duration") or audio_stream.get("duration", "0")
    try:
        duration = float(duration_str)
    except (ValueError, TypeError):
        duration = 0.0

    codec = audio_stream.get("codec_name", "unknown")
    sample_rate = audio_stream.get("sample_rate", "unknown")
    channels = audio_stream.get("channels", 0)
    bit_rate = audio_stream.get("bit_rate") or fmt.get("bit_rate", "unknown")

    return {
        "has_audio": True,
        "duration": round(duration, 1),
        "codec": codec,
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_rate": bit_rate,
    }


class AudioProvider(BaseProvider):
    """Audio metadata extraction provider via ffprobe."""

    name = "audio_check"

    def is_configured(self) -> bool:
        return shutil.which("ffprobe") is not None

    async def scan(self, file_path: str) -> ProviderResult:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _AUDIO_EXT:
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        start = time.monotonic()
        try:
            info = await _get_audio_info(file_path)
            elapsed = (time.monotonic() - start) * 1000

            if "error" in info:
                return ProviderResult(
                    provider=self.name, is_nsfw=False, error=True,
                    labels=[f"error:{info['error']}"],
                    latency_ms=round(elapsed, 1),
                )

            if not info.get("has_audio", False):
                return ProviderResult(
                    provider=self.name, is_nsfw=False, skipped=True,
                    labels=["no_audio_stream"],
                    latency_ms=round(elapsed, 1),
                )

            # Build informational labels
            labels = [
                "audio_detected",
                f"duration:{info['duration']}s",
                f"codec:{info['codec']}",
                f"channels:{info['channels']}",
            ]
            if info.get("sample_rate") != "unknown":
                labels.append(f"sample_rate:{info['sample_rate']}")

            # Currently does not flag as NSFW — metadata only.
            # Future: integrate speech-to-text for toxicity detection.
            return ProviderResult(
                provider=self.name,
                is_nsfw=False,
                confidence=0.0,
                labels=labels,
                latency_ms=round(elapsed, 1),
            )

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("Audio provider error: %s", e)
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
