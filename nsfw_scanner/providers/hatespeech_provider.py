"""Hate speech detection provider using dehatebert-mono-english.

Extracts text from image EXIF metadata or filename and classifies it
as HATE or NON-HATE using a fine-tuned BERT model.
"""
import asyncio
import os
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from transformers import pipeline
        _pipeline = pipeline(
            "text-classification",
            model="Hate-speech-CNERG/dehatebert-mono-english",
        )
    return _pipeline


def _extract_text(file_path: str) -> str:
    """Extract text from EXIF metadata or fall back to filename."""
    try:
        from PIL import Image
        img = Image.open(file_path)
        exif = img.getexif()
        for tag in [270, 40091]:  # ImageDescription, XPComment
            val = exif.get(tag)
            if val and isinstance(val, str):
                return val
    except Exception:
        pass
    return os.path.splitext(os.path.basename(file_path))[0].replace("_", " ")


def _scan_sync(file_path: str) -> dict:
    pipe = _get_pipeline()
    text = _extract_text(file_path)

    if not text or not text.strip():
        return {"is_nsfw": False, "confidence": 0.0, "labels": []}

    results = pipe(text)
    if not results:
        return {"is_nsfw": False, "confidence": 0.0, "labels": []}

    top = results[0]
    label = top["label"]   # "HATE" or "NON-HATE"
    score = top["score"]

    is_nsfw = label == "HATE" and score > 0.7
    labels = [f"hate_speech:{score:.2f}"] if is_nsfw else []
    confidence = score if is_nsfw else 0.0

    return {"is_nsfw": is_nsfw, "confidence": confidence, "labels": labels}


class HateSpeechProvider(BaseProvider):
    name = "hate_speech"

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
