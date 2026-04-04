"""YOLOv8 weapon detection provider using Ultralytics.

Detects weapons (guns, knives, etc.) in images using a custom-trained
YOLOv8 model.  Flags the image if any weapon class is detected with
confidence above the threshold.
"""
import asyncio
import os
import time
import logging

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".heif"}

CONFIDENCE_THRESHOLD = 0.4
DEFAULT_MODEL_PATH = "/app/data/weapon_yolov8n.pt"


class YOLOWeaponProvider(BaseProvider):
    """Weapon detection using a YOLOv8 model."""

    name = "yolo_weapons"

    def __init__(self):
        self._model = None

    def _get_model_path(self) -> str:
        return os.environ.get("YOLO_WEAPONS_MODEL_PATH", DEFAULT_MODEL_PATH)

    def is_configured(self) -> bool:
        try:
            import ultralytics  # noqa: F401
        except ImportError:
            logger.debug("yolo_weapons: ultralytics not installed")
            return False

        model_path = self._get_model_path()
        if not os.path.isfile(model_path):
            logger.warning(
                "yolo_weapons: model file not found at %s — "
                "set YOLO_WEAPONS_MODEL_PATH to the correct path",
                model_path,
            )
            return False
        return True

    def _get_model(self):
        """Lazy-load the YOLO model on first use."""
        if self._model is None:
            from ultralytics import YOLO
            model_path = self._get_model_path()
            logger.info("Loading YOLOv8 weapon model from: %s", model_path)
            self._model = YOLO(model_path)
            logger.info("YOLOv8 weapon model loaded successfully")
        return self._model

    def _scan_sync(self, file_path: str) -> tuple[list[str], float]:
        """Run YOLO inference and collect detections above threshold."""
        model = self._get_model()
        results = model(file_path, verbose=False)
        labels = []
        max_conf = 0.0
        for r in results:
            for box in r.boxes:
                cls_name = r.names[int(box.cls)]
                conf = float(box.conf)
                if conf > CONFIDENCE_THRESHOLD:
                    labels.append(f"{cls_name}:{conf:.2f}")
                    max_conf = max(max_conf, conf)
        return labels, max_conf

    async def scan(self, file_path: str) -> ProviderResult:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _IMAGE_EXT:
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        start = time.monotonic()
        try:
            labels, max_conf = await asyncio.to_thread(self._scan_sync, file_path)
            elapsed = (time.monotonic() - start) * 1000
            is_nsfw = max_conf > CONFIDENCE_THRESHOLD
            return ProviderResult(
                provider=self.name,
                is_nsfw=is_nsfw,
                confidence=round(max_conf, 3) if is_nsfw else 0.0,
                labels=labels,
                latency_ms=round(elapsed, 1),
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("YOLOWeapon provider error: %s", e)
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
