"""NSFWJS / nsfw_model local ONNX provider (free, offline).

Uses the MobileNet v2 ONNX model to classify images into 5 classes:
Porn, Sexy, Hentai, Drawing, Neutral.

The model file must be provided at a local path (default: /app/data/nsfwjs_model.onnx).
Set the NSFWJS_MODEL_PATH environment variable to override the location.
"""
import asyncio
import os
import time
import logging
from typing import Optional

import numpy as np
from PIL import Image

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_PATH = "/app/data/nsfwjs_model.onnx"
_MODEL_PATH = os.environ.get("NSFWJS_MODEL_PATH", _DEFAULT_MODEL_PATH)
_INPUT_SIZE = 224

# The model outputs probabilities in this fixed order
_CLASS_NAMES = ["Drawing", "Hentai", "Neutral", "Porn", "Sexy"]

_session = None


def _model_exists() -> bool:
    """Check whether the ONNX model file exists and is non-empty."""
    return os.path.exists(_MODEL_PATH) and os.path.getsize(_MODEL_PATH) > 0


def _ensure_model() -> str:
    """Verify the ONNX model exists at the configured path. Returns the path."""
    if _model_exists():
        return _MODEL_PATH
    raise FileNotFoundError(
        f"NSFWJS model not found at {_MODEL_PATH} — download it manually "
        f"or set NSFWJS_MODEL_PATH to the correct location."
    )


def _get_session():
    """Lazy-load the ONNX Runtime inference session (singleton)."""
    global _session
    if _session is None:
        import onnxruntime as ort
        model_path = _ensure_model()
        _session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
    return _session


def _preprocess(file_path: str) -> np.ndarray:
    """Load an image, resize to 224x224, normalize to [0, 1] float32."""
    img = Image.open(file_path).convert("RGB")
    img = img.resize((_INPUT_SIZE, _INPUT_SIZE), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    # Add batch dimension: (1, 224, 224, 3)
    return np.expand_dims(arr, axis=0)


def _scan_sync(file_path: str) -> dict:
    """Run ONNX inference and decide NSFW / Safe."""
    session = _get_session()
    input_data = _preprocess(file_path)

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    (probs,) = session.run([output_name], {input_name: input_data})

    # probs shape: (1, 5) — squeeze to 1-D
    scores = probs[0]
    class_scores = {name: float(scores[i]) for i, name in enumerate(_CLASS_NAMES)}

    porn = class_scores.get("Porn", 0.0)
    sexy = class_scores.get("Sexy", 0.0)
    hentai = class_scores.get("Hentai", 0.0)

    is_nsfw = porn > 0.5 or (sexy > 0.7 and hentai > 0.5)

    # Build labels for flagged classes (sorted by score descending)
    labels = []
    for name in sorted(class_scores, key=class_scores.get, reverse=True):
        score = class_scores[name]
        if name in ("Porn", "Sexy", "Hentai") and score > 0.1:
            labels.append(f"{name.lower()}:{score:.2f}")

    confidence = max(porn, sexy, hentai)

    return {
        "is_nsfw": is_nsfw,
        "confidence": confidence,
        "labels": labels,
    }


class NsfwjsProvider(BaseProvider):
    name = "nsfwjs"

    def is_configured(self) -> bool:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            logger.warning("onnxruntime is not installed — NSFWJS provider disabled.")
            return False
        if not _model_exists():
            logger.warning(
                "NSFWJS model not found at %s — download it manually or set "
                "NSFWJS_MODEL_PATH.",
                _MODEL_PATH,
            )
            return False
        return True

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
