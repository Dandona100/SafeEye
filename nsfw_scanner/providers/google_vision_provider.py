"""Google Cloud Vision SafeSearch provider."""
import os
import asyncio
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_LIKELIHOOD_SCORE = {0: 0, 1: 0.1, 2: 0.2, 3: 0.5, 4: 0.8, 5: 0.95}


def _scan_sync(file_path: str) -> dict:
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    with open(file_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)
    response = client.safe_search_detection(image=image)
    safe = response.safe_search_annotation

    labels = []
    max_conf = 0.0

    adult = _LIKELIHOOD_SCORE.get(safe.adult, 0)
    if adult >= 0.5:
        labels.append(f"adult:{adult:.2f}")
        max_conf = max(max_conf, adult)

    violence = _LIKELIHOOD_SCORE.get(safe.violence, 0)
    if violence >= 0.5:
        labels.append(f"violence:{violence:.2f}")
        max_conf = max(max_conf, violence)

    racy = _LIKELIHOOD_SCORE.get(safe.racy, 0)
    if racy >= 0.8:
        labels.append(f"racy:{racy:.2f}")
        max_conf = max(max_conf, racy)

    return {"is_nsfw": len(labels) > 0, "confidence": max_conf, "labels": labels}


class GoogleVisionProvider(BaseProvider):
    name = "google_vision"

    def is_configured(self) -> bool:
        creds = os.environ.get("GOOGLE_VISION_CREDENTIALS", "")
        if creds:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
        try:
            import google.cloud.vision  # noqa: F401
            return bool(creds)
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
