"""NudeNet local scanning provider (free, offline)."""
import asyncio
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_NSFW_LABELS_HIGH = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
}
_NSFW_LABELS_MEDIUM = {
    "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED",
}
_THRESHOLD_HIGH = 0.45
_THRESHOLD_MEDIUM = 0.65
_MEDIUM_ACCUMULATION_COUNT = 2

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        from nudenet import NudeDetector
        _detector = NudeDetector()
    return _detector


def _scan_sync(file_path: str) -> dict:
    detector = _get_detector()
    detections = detector.detect(file_path)
    nsfw_labels = []
    max_confidence = 0.0
    medium_count = 0

    for det in detections:
        label = det.get("class", "")
        score = det.get("score", 0)
        if label in _NSFW_LABELS_HIGH and score >= _THRESHOLD_HIGH:
            nsfw_labels.append(f"{label}:{score:.2f}")
            max_confidence = max(max_confidence, score)
        elif label in _NSFW_LABELS_MEDIUM and score >= _THRESHOLD_MEDIUM:
            nsfw_labels.append(f"{label}:{score:.2f}")
            max_confidence = max(max_confidence, score)
            medium_count += 1

    has_high = any(l.split(":")[0] in _NSFW_LABELS_HIGH for l in nsfw_labels)
    is_nsfw = has_high or medium_count >= _MEDIUM_ACCUMULATION_COUNT

    if not is_nsfw and medium_count == 1:
        nsfw_labels = []
        max_confidence = 0.0

    return {"is_nsfw": is_nsfw, "confidence": max_confidence, "labels": nsfw_labels}


class NudeNetProvider(BaseProvider):
    name = "nudenet"

    def is_configured(self) -> bool:
        try:
            import nudenet  # noqa: F401
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
