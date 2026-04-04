"""Detoxify text toxicity provider — scans EXIF metadata and filenames."""
import asyncio
import os
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_model = None

_TOXICITY_CATEGORIES = [
    "toxicity", "severe_toxicity", "obscene", "threat", "insult", "identity_attack",
]
_THRESHOLD = 0.7


def _get_model():
    global _model
    if _model is None:
        from detoxify import Detoxify
        _model = Detoxify("original")
    return _model


def _extract_text_from_file(file_path: str) -> str | None:
    """Extract text from image EXIF data, falling back to filename."""
    try:
        from PIL import Image
        img = Image.open(file_path)
        exif = img.getexif()
        texts = []
        # EXIF description tags: ImageDescription, XPComment, XPSubject
        for tag in [270, 40091, 40092]:
            val = exif.get(tag)
            if val and isinstance(val, str):
                texts.append(val)
        if texts:
            return " ".join(texts)
    except Exception:
        pass
    # Fallback: use filename
    basename = os.path.splitext(os.path.basename(file_path))[0]
    text = basename.replace("_", " ").replace("-", " ")
    # Skip very short or generic filenames (likely hashes/IDs)
    if len(text) < 5 or not any(c.isalpha() for c in text):
        return None
    return text


def _scan_sync(file_path: str) -> dict:
    text = _extract_text_from_file(file_path)
    if not text:
        return {"skipped": True}

    model = _get_model()
    results = model.predict(text)

    flagged_labels = []
    max_confidence = 0.0
    is_nsfw = False

    for category in _TOXICITY_CATEGORIES:
        score = results.get(category, 0.0)
        if score > _THRESHOLD:
            flagged_labels.append(f"{category}:{score:.2f}")
            max_confidence = max(max_confidence, score)
            is_nsfw = True

    return {
        "is_nsfw": is_nsfw,
        "confidence": max_confidence,
        "labels": flagged_labels,
        "skipped": False,
    }


class DetoxifyProvider(BaseProvider):
    name = "detoxify"

    def is_configured(self) -> bool:
        try:
            import detoxify  # noqa: F401
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
            if result.get("skipped"):
                return ProviderResult(
                    provider=self.name, is_nsfw=False, skipped=True,
                    latency_ms=round(elapsed, 1),
                )
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
