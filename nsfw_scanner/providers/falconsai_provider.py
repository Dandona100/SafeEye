"""Falconsai NSFW image detection provider using a ViT-based classifier.

Uses the Falconsai/nsfw_image_detection model via Hugging Face transformers
for high-accuracy (98%) NSFW classification.
"""
import asyncio
import os
import time
import logging

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".heif"}

NSFW_THRESHOLD = 0.5
MODEL_ID = "Falconsai/nsfw_image_detection"


class FalconsaiProvider(BaseProvider):
    """NSFW detection using the Falconsai ViT-based classifier."""

    name = "falconsai_nsfw"

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
            logger.info("Loading Falconsai NSFW model: %s", MODEL_ID)
            self._pipeline = pipeline("image-classification", model=MODEL_ID)
            logger.info("Falconsai NSFW model loaded successfully")
        return self._pipeline

    def _scan_sync(self, file_path: str) -> dict:
        """Run inference synchronously (called via asyncio.to_thread)."""
        pipe = self._get_pipeline()
        results = pipe(file_path)
        # Output: [{'label': 'nsfw', 'score': ...}, {'label': 'normal', 'score': ...}]

        scores = {item["label"]: item["score"] for item in results}
        nsfw_score = scores.get("nsfw", 0.0)

        is_nsfw = nsfw_score > NSFW_THRESHOLD
        labels = [f"nsfw:{nsfw_score:.2f}"] if is_nsfw else []

        return {
            "is_nsfw": is_nsfw,
            "confidence": round(nsfw_score, 3) if is_nsfw else 0.0,
            "labels": labels,
        }

    async def scan(self, file_path: str) -> ProviderResult:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _IMAGE_EXT:
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
            logger.error("Falconsai provider error: %s", e)
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
