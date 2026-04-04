"""Freepik NSFW image detector provider (4-level classification)."""
import asyncio
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from transformers import pipeline
        _pipeline = pipeline(
            "image-classification",
            model="Freepik/nsfw_image_detector",
        )
    return _pipeline


def _scan_sync(file_path: str) -> dict:
    from PIL import Image

    pipe = _get_pipeline()
    img = Image.open(file_path).convert("RGB")
    results = pipe(img)

    # Results: list of {"label": "neutral"|"low"|"medium"|"high", "score": float}
    scores = {r["label"]: r["score"] for r in results}

    high_score = scores.get("high", 0.0)
    medium_score = scores.get("medium", 0.0)

    is_nsfw = high_score > 0.5 or medium_score > 0.7

    labels = []
    if high_score > 0.1:
        labels.append(f"high:{high_score:.2f}")
    if medium_score > 0.1:
        labels.append(f"medium:{medium_score:.2f}")

    confidence = max(high_score, medium_score) if is_nsfw else 0.0

    return {"is_nsfw": is_nsfw, "confidence": confidence, "labels": labels}


class FreepikNsfwProvider(BaseProvider):
    name = "freepik_nsfw"

    def is_configured(self) -> bool:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)
        start = time.monotonic()
        try:
            result = await asyncio.to_thread(_scan_sync, file_path)
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
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
