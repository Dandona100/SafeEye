"""Bumble Private Detector provider using a TensorFlow SavedModel.

Classifies images as lewd/safe using a pre-trained model originally designed
for detecting intimate content. Requires a local SavedModel directory.
"""
import asyncio
import os
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

BUMBLE_MODEL_PATH = os.environ.get("BUMBLE_MODEL_PATH", "/app/data/bumble_model")

_model = None


def _get_model():
    global _model
    if _model is None:
        import tensorflow as tf
        _model = tf.saved_model.load(BUMBLE_MODEL_PATH)
    return _model


def _scan_sync(file_path: str) -> dict:
    import tensorflow as tf
    from PIL import Image
    import numpy as np

    model = _get_model()
    img = Image.open(file_path).convert("RGB").resize((480, 480))
    img_array = np.expand_dims(np.array(img) / 255.0, axis=0).astype(np.float32)
    prediction = model(tf.constant(img_array))
    score = float(prediction[0])

    is_nsfw = score > 0.5
    labels = [f"lewd:{score:.2f}"] if is_nsfw else []
    confidence = score if is_nsfw else 0.0

    return {"is_nsfw": is_nsfw, "confidence": confidence, "labels": labels}


class BumblePrivateProvider(BaseProvider):
    name = "bumble_private"

    def is_configured(self) -> bool:
        try:
            import tensorflow  # noqa: F401
        except ImportError:
            return False
        return os.path.isdir(BUMBLE_MODEL_PATH)

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
