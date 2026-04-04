"""SigLIP2 multi-class NSFW image classification provider.

Uses the Guard-Against-Unsafe-Content-Siglip2 model to classify images into
five categories: Anime Picture, Hentai, Normal, Pornography, Enticing/Sensual.
Particularly effective at detecting anime/hentai content that other models miss.
"""
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
            model="prithivMLmods/Guard-Against-Unsafe-Content-Siglip2",
        )
    return _pipeline


def _scan_sync(file_path: str) -> dict:
    from PIL import Image

    pipe = _get_pipeline()
    img = Image.open(file_path).convert("RGB")
    results = pipe(img)

    # Build score map from model output
    scores = {item["label"]: item["score"] for item in results}

    porn_score = scores.get("Pornography", 0.0)
    hentai_score = scores.get("Hentai", 0.0)

    is_nsfw = porn_score > 0.5 or hentai_score > 0.5

    labels = []
    if porn_score > 0.5:
        labels.append(f"pornography:{porn_score:.2f}")
    if hentai_score > 0.5:
        labels.append(f"hentai:{hentai_score:.2f}")

    max_conf = max(porn_score, hentai_score) if is_nsfw else 0.0

    return {"is_nsfw": is_nsfw, "confidence": max_conf, "labels": labels}


class SigLIPNsfwProvider(BaseProvider):
    name = "siglip_nsfw"

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
