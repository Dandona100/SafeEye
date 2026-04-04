"""Basic deepfake detection provider using face consistency heuristics.

Compares face region histograms between consecutive video frames.
If correlation drops below threshold in many frame pairs, flags as suspicious.
This is a lightweight heuristic, not a full deepfake detection model.
"""
import asyncio
import os
import time
import logging

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_VIDEO_EXT = {".mp4", ".webm", ".avi", ".mkv", ".mov"}
_SAMPLE_FRAMES = 10
_HISTOGRAM_CORRELATION_THRESHOLD = 0.6
_SUSPICIOUS_PAIR_RATIO = 0.3


def _analyze_video_sync(file_path: str) -> dict:
    """Extract frames, detect faces, compare histograms between consecutive frames."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        return {"is_suspicious": False, "labels": ["error:cannot_open_video"]}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames < 2:
        cap.release()
        return {"is_suspicious": False, "labels": ["too_few_frames"]}

    # Calculate frame indices to sample evenly across the video
    sample_count = min(_SAMPLE_FRAMES, total_frames)
    indices = [int(i * total_frames / (sample_count + 1)) for i in range(1, sample_count + 1)]

    # Load the Haar cascade for face detection
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)
    if face_cascade.empty():
        cap.release()
        return {"is_suspicious": False, "labels": ["error:cascade_not_found"]}

    face_histograms = []
    frames_with_faces = 0

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.3, minNeighbors=5)

        if len(faces) == 0:
            face_histograms.append(None)
            continue

        frames_with_faces += 1

        # Use the largest face
        largest = max(faces, key=lambda f: f[2] * f[3])
        x, y, w, h = largest
        face_roi = frame[y:y + h, x:x + w]

        # Compute colour histogram for face region (H and S channels in HSV)
        hsv = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        face_histograms.append(hist)

    cap.release()

    # Need at least 2 frames with faces to compare
    if frames_with_faces < 2:
        return {
            "is_suspicious": False,
            "labels": [f"faces_found:{frames_with_faces}"],
            "confidence": 0.0,
        }

    # Compare consecutive face histograms
    inconsistent_pairs = 0
    compared_pairs = 0

    for i in range(len(face_histograms) - 1):
        hist_a = face_histograms[i]
        hist_b = face_histograms[i + 1]
        if hist_a is None or hist_b is None:
            continue

        compared_pairs += 1
        correlation = cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL)
        if correlation < _HISTOGRAM_CORRELATION_THRESHOLD:
            inconsistent_pairs += 1

    if compared_pairs == 0:
        return {
            "is_suspicious": False,
            "labels": [f"faces_found:{frames_with_faces}", "no_consecutive_pairs"],
            "confidence": 0.0,
        }

    inconsistency_ratio = inconsistent_pairs / compared_pairs
    is_suspicious = inconsistency_ratio > _SUSPICIOUS_PAIR_RATIO

    # Map ratio to a 0-1 confidence score
    confidence = min(1.0, inconsistency_ratio / 0.8) if is_suspicious else 0.0

    labels = [
        f"faces_found:{frames_with_faces}",
        f"inconsistent_pairs:{inconsistent_pairs}/{compared_pairs}",
        f"inconsistency_ratio:{inconsistency_ratio:.2f}",
    ]
    if is_suspicious:
        labels.insert(0, "deepfake_suspicious")

    return {
        "is_suspicious": is_suspicious,
        "confidence": round(confidence, 3),
        "labels": labels,
    }


class DeepfakeProvider(BaseProvider):
    """Basic deepfake detection via face histogram consistency."""

    name = "deepfake_check"

    def is_configured(self) -> bool:
        try:
            import cv2  # noqa: F401
            return True
        except ImportError:
            return False

    async def scan(self, file_path: str) -> ProviderResult:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _VIDEO_EXT:
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        start = time.monotonic()
        try:
            result = await asyncio.to_thread(_analyze_video_sync, file_path)
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name,
                is_nsfw=result["is_suspicious"],
                confidence=result.get("confidence", 0.0),
                labels=result.get("labels", []),
                latency_ms=round(elapsed, 1),
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("Deepfake provider error: %s", e)
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
