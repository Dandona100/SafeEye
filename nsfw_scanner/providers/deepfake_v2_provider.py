"""Deep Fake Detector v2 provider using prithivMLmods/Deep-Fake-Detector-v2-Model.

Uses a Hugging Face transformers image-classification pipeline to detect
deepfake images.  Videos are skipped — the existing deepfake_provider handles
those via histogram heuristics.
"""
import asyncio
import os
import time
import logging

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".heif"}
_VIDEO_EXT = {".mp4", ".webm", ".avi", ".mkv", ".mov", ".flv", ".3gp", ".m4v"}

DEEPFAKE_THRESHOLD = 0.6
MODEL_ID = "prithivMLmods/Deep-Fake-Detector-v2-Model"


class DeepfakeV2Provider(BaseProvider):
    """Deepfake detection for images using a ViT-based classifier."""

    name = "deepfake_v2"

    def __init__(self):
        self._pipeline = None

    def is_configured(self) -> bool:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_pipeline(self):
        """Lazy-load the classification pipeline on first use."""
        if self._pipeline is None:
            from transformers import pipeline
            logger.info("Loading Deep-Fake-Detector-v2 model: %s", MODEL_ID)
            self._pipeline = pipeline("image-classification", model=MODEL_ID)
            logger.info("Deep-Fake-Detector-v2 model loaded successfully")
        return self._pipeline

    def _scan_sync(self, file_path: str) -> dict:
        """Run inference synchronously (called via asyncio.to_thread)."""
        pipe = self._get_pipeline()
        results = pipe(file_path)
        # Output: [{'label': 'Realism', 'score': ...}, {'label': 'Deepfake', 'score': ...}]

        scores = {item["label"]: item["score"] for item in results}
        deepfake_score = scores.get("Deepfake", 0.0)
        realism_score = scores.get("Realism", 0.0)

        is_deepfake = deepfake_score > DEEPFAKE_THRESHOLD

        if is_deepfake:
            labels = [f"deepfake:{deepfake_score:.2f}"]
        else:
            labels = [f"real:{realism_score:.2f}"]

        return {
            "is_nsfw": is_deepfake,
            "confidence": round(deepfake_score, 3) if is_deepfake else 0.0,
            "labels": labels,
        }

    async def scan(self, file_path: str) -> ProviderResult:
        ext = os.path.splitext(file_path)[1].lower()

        # Images only — skip videos (existing deepfake_provider handles those)
        if ext in _VIDEO_EXT or ext not in _IMAGE_EXT:
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        start = time.monotonic()
        try:
            result = await asyncio.to_thread(self._scan_sync, file_path)
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name,
                is_nsfw=result["is_nsfw"],
                confidence=result["confidence"],
                labels=result["labels"],
                latency_ms=round(elapsed, 1),
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("DeepfakeV2 provider error: %s", e)
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
