"""Multi-provider parallel scanner with voting aggregation."""
import asyncio
import os
import time
import uuid
import random
import logging
from typing import Optional
from PIL import Image

from nsfw_scanner.models import ProviderResult, AggregatedResult
from nsfw_scanner.providers.nudenet_provider import NudeNetProvider
from nsfw_scanner.providers.sightengine_provider import SightengineProvider
from nsfw_scanner.providers.google_vision_provider import GoogleVisionProvider
from nsfw_scanner.providers.moderatecontent_provider import ModerateContentProvider
from nsfw_scanner.providers.amazon_rekognition_provider import AmazonRekognitionProvider
from nsfw_scanner.providers.azure_provider import AzureContentSafetyProvider
from nsfw_scanner.providers.picpurify_provider import PicPurifyProvider
from nsfw_scanner.providers.nsfwjs_provider import NsfwjsProvider
from nsfw_scanner.providers.deepfake_provider import DeepfakeProvider
from nsfw_scanner.providers.audio_provider import AudioProvider
from nsfw_scanner.providers.clip_provider import CLIPProvider
from nsfw_scanner.providers.marqo_nsfw_provider import MarqoNsfwProvider
from nsfw_scanner.providers.detoxify_provider import DetoxifyProvider
from nsfw_scanner.providers.freepik_nsfw_provider import FreepikNsfwProvider
from nsfw_scanner.providers.deepfake_v2_provider import DeepfakeV2Provider
from nsfw_scanner.providers.yolo_weapon_provider import YOLOWeaponProvider
from nsfw_scanner.providers.falconsai_provider import FalconsaiProvider
from nsfw_scanner.providers.siglip_nsfw_provider import SigLIPNsfwProvider
from nsfw_scanner.providers.bumble_provider import BumblePrivateProvider
from nsfw_scanner.providers.hatespeech_provider import HateSpeechProvider
from nsfw_scanner.plugin_loader import load_plugins

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT = int(os.environ.get("PROVIDER_TIMEOUT_SECONDS", "15"))

# Provider trust weights for confidence averaging
_WEIGHTS = {
    "nudenet": 1.0, "sightengine": 1.2, "google_vision": 1.1,
    "moderatecontent": 0.9, "amazon_rekognition": 1.2,
    "azure_content_safety": 1.2, "picpurify": 1.1,
    "nsfwjs": 1.0,
    "deepfake_check": 0.8,
    "audio_check": 0.5,
    "clip_search": 1.1,
    "marqo_nsfw": 1.0,
    "detoxify": 0.6,
    "freepik_nsfw": 1.1,
    "deepfake_v2": 1.0,
    "yolo_weapons": 1.1,
    "falconsai_nsfw": 1.0,
    "siglip_nsfw": 1.0,
    "bumble_private": 1.1,
    "hate_speech": 0.6,
}

def compute_phash(file_path: str, hash_size: int = 8) -> str | None:
    """Compute perceptual hash of an image.

    Uses a simplified dHash-style approach: resize to (hash_size+1, hash_size),
    convert to greyscale, compare each pixel to the average, and encode as hex.
    """
    try:
        img = Image.open(file_path).convert('L').resize(
            (hash_size + 1, hash_size), Image.LANCZOS,
        )
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = ''.join('1' if p > avg else '0' for p in pixels)
        return hex(int(bits, 2))[2:].zfill(hash_size ** 2 // 4)
    except Exception:
        return None


def compare_images(file_a: str, file_b: str) -> dict:
    """Compare two images — pHash distance, pixel diff %, structural similarity."""
    try:
        img_a = Image.open(file_a).convert('RGB')
        img_b = Image.open(file_b).convert('RGB')

        # Resize to same dimensions
        size = (max(img_a.width, img_b.width), max(img_a.height, img_b.height))
        img_a = img_a.resize(size, Image.LANCZOS)
        img_b = img_b.resize(size, Image.LANCZOS)

        # pHash distance
        hash_a = compute_phash(file_a)
        hash_b = compute_phash(file_b)
        hamming = 0
        if hash_a and hash_b:
            xor = int(hash_a, 16) ^ int(hash_b, 16)
            hamming = bin(xor).count('1')

        # Pixel diff
        import numpy as np
        arr_a = np.array(img_a, dtype=np.float32)
        arr_b = np.array(img_b, dtype=np.float32)
        diff = np.abs(arr_a - arr_b)
        diff_pct = round((diff > 25).mean() * 100, 2)  # pixels with >25/255 diff

        # Generate diff image (red highlights)
        diff_mask = (diff.mean(axis=2) > 25).astype(np.uint8) * 255
        diff_img = Image.fromarray(diff_mask, mode='L')

        # Save diff image to temp
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        diff_img.save(tmp.name)

        # Similarity score (0-100, 100 = identical)
        similarity = round(100 - diff_pct, 2)

        return {
            "phash_a": hash_a,
            "phash_b": hash_b,
            "hamming_distance": hamming,
            "pixel_diff_pct": diff_pct,
            "similarity": similarity,
            "diff_image_path": tmp.name,
            "identical": hamming == 0 and diff_pct < 0.1,
        }
    except Exception as e:
        return {"error": str(e)}


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".heif"}
_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".3gp", ".m4v"}
_VIDEO_SAMPLE_FRAMES = 30

# Singleton provider instances
_providers = None


def _get_providers():
    global _providers
    if _providers is None:
        _providers = [
            NudeNetProvider(), SightengineProvider(), GoogleVisionProvider(),
            ModerateContentProvider(), AmazonRekognitionProvider(),
            AzureContentSafetyProvider(), PicPurifyProvider(),
            NsfwjsProvider(),
            DeepfakeProvider(),
            AudioProvider(),
            CLIPProvider(),
            MarqoNsfwProvider(),
            DetoxifyProvider(),
            FreepikNsfwProvider(),
            DeepfakeV2Provider(),
            YOLOWeaponProvider(),
            FalconsaiProvider(),
            SigLIPNsfwProvider(),
            BumblePrivateProvider(),
            HateSpeechProvider(),
        ]
        # Load custom providers from plugins directory
        plugins_dir = os.environ.get("SAFEEYE_PLUGINS_DIR", "/app/plugins")
        plugins = load_plugins(plugins_dir)
        if plugins:
            _providers.extend(plugins)
            logger.info("Loaded %d plugin provider(s): %s",
                        len(plugins), [p.name for p in plugins])
    return _providers


_disabled_providers: set[str] = set()


def load_disabled_providers(disabled: set[str]):
    """Load disabled provider names (called from app startup)."""
    global _disabled_providers
    _disabled_providers = disabled


def get_active_providers() -> list[str]:
    active = []
    for p in _get_providers():
        try:
            if p.is_configured() and p.name not in _disabled_providers:
                active.append(p.name)
        except Exception:
            pass
    return active


def get_all_providers_status() -> list[dict]:
    """Return status of all providers with details."""
    results = []
    for p in _get_providers():
        try:
            configured = p.is_configured()
        except Exception:
            configured = False
        results.append({
            "name": p.name,
            "configured": configured,
            "disabled": p.name in _disabled_providers,
            "active": p.is_configured() and p.name not in _disabled_providers,
        })
    return results


async def scan_file(file_path: str) -> AggregatedResult:
    """Scan a file with all configured providers in parallel."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext in _IMAGE_EXT:
        return await _scan_image_parallel(file_path)
    elif ext in _VIDEO_EXT:
        return await _scan_video(file_path)
    else:
        return AggregatedResult(
            is_nsfw=False, scan_id=uuid.uuid4().hex[:16],
            scan_duration_ms=0, providers_total=0,
        )


async def _scan_image_parallel(file_path: str) -> AggregatedResult:
    """Run all providers on a single image in parallel, aggregate."""
    scan_id = uuid.uuid4().hex[:16]
    providers = [p for p in _get_providers() if p.is_configured() and p.name not in _disabled_providers]

    if not providers:
        return AggregatedResult(is_nsfw=False, scan_id=scan_id, providers_total=0)

    async def _timed_scan(provider, path):
        try:
            return await asyncio.wait_for(provider.scan(path), timeout=PROVIDER_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"Provider {provider.name} timed out after {PROVIDER_TIMEOUT}s")
            return ProviderResult(
                provider=provider.name, is_nsfw=False,
                error=True, labels=[f"timeout:{PROVIDER_TIMEOUT}s"],
            )

    start = time.monotonic()
    results: list[ProviderResult] = await asyncio.gather(
        *[_timed_scan(p, file_path) for p in providers],
        return_exceptions=True,
    )

    # Handle exceptions from gather
    clean_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            clean_results.append(ProviderResult(
                provider=providers[i].name, is_nsfw=False,
                error=True, labels=[f"error:{r}"],
            ))
        else:
            clean_results.append(r)

    elapsed = (time.monotonic() - start) * 1000
    agg = _aggregate(clean_results)
    agg.scan_id = scan_id
    agg.scan_duration_ms = round(elapsed, 1)
    agg.phash = compute_phash(file_path)
    return agg


async def _scan_video(file_path: str) -> AggregatedResult:
    """Extract frames from video, scan each, aggregate across all frames."""
    try:
        import cv2
    except ImportError:
        return AggregatedResult(
            is_nsfw=False, scan_id=uuid.uuid4().hex[:16],
            labels=["opencv_not_available"], providers_total=0,
        )

    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        return AggregatedResult(is_nsfw=False, scan_id=uuid.uuid4().hex[:16], providers_total=0)

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    duration = total_frames / fps if fps > 0 else 0

    if total_frames < 1:
        cap.release()
        return AggregatedResult(is_nsfw=False, scan_id=uuid.uuid4().hex[:16], providers_total=0)

    # Scale frames with duration: ~1 frame per 5 seconds, min 6, max 30
    ideal = max(6, int(duration / 5))
    sample_count = min(ideal, _VIDEO_SAMPLE_FRAMES, total_frames)

    segment_size = total_frames // (sample_count + 1)
    frame_indices = []
    for i in range(1, sample_count + 1):
        base = int(i * total_frames / (sample_count + 1))
        jitter = random.randint(0, max(1, segment_size // 3))
        frame_indices.append(min(base + jitter, total_frames - 1))

    temp_dir = os.path.dirname(file_path) or "/tmp"
    scan_id = uuid.uuid4().hex[:16]
    start = time.monotonic()

    all_provider_results: list[ProviderResult] = []
    last_frame_results: list[ProviderResult] = []
    nsfw_frame_count = 0
    frames_scanned = 0

    for idx in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            continue

        frame_path = os.path.join(temp_dir, f"_nsfw_{scan_id}_{idx}.jpg")
        try:
            cv2.imwrite(frame_path, frame)
            frame_result = await _scan_image_parallel(frame_path)
            frames_scanned += 1
            last_frame_results = frame_result.provider_results

            if frame_result.is_nsfw:
                nsfw_frame_count += 1
                all_provider_results.extend(frame_result.provider_results)
                if frame_result.confidence >= 0.8:
                    break
        finally:
            if os.path.exists(frame_path):
                os.remove(frame_path)

    cap.release()
    elapsed = (time.monotonic() - start) * 1000

    if nsfw_frame_count > 0:
        agg = _aggregate(all_provider_results)
    else:
        # No NSFW found — still return provider info from last frame
        agg = _aggregate(last_frame_results) if last_frame_results else AggregatedResult(
            is_nsfw=False, providers_total=len([p for p in _get_providers() if p.is_configured()]),
        )

    agg.scan_id = scan_id
    agg.scan_duration_ms = round(elapsed, 1)
    return agg


def _aggregate(results: list[ProviderResult]) -> AggregatedResult:
    """Voting + weighted confidence aggregation."""
    active = [r for r in results if not r.skipped and not r.error]
    flagged = [r for r in active if r.is_nsfw]

    providers_total = len(active)
    providers_agree = len(flagged)

    if providers_total == 0:
        return AggregatedResult(
            is_nsfw=False, providers_total=0, providers_agree=0,
            provider_results=results,
        )

    # Voting rules
    high_conf = any(r.confidence >= 0.75 for r in flagged)
    majority = providers_agree >= 2
    single_low = providers_agree == 1 and not high_conf

    is_nsfw = high_conf or majority
    borderline = single_low and not is_nsfw

    # Weighted confidence
    if flagged:
        total_weight = sum(_WEIGHTS.get(r.provider, 1.0) for r in flagged)
        weighted_sum = sum(r.confidence * _WEIGHTS.get(r.provider, 1.0) for r in flagged)
        confidence = weighted_sum / total_weight if total_weight > 0 else 0
    else:
        confidence = 0.0

    # Merge labels
    all_labels = []
    for r in flagged:
        all_labels.extend(r.labels)
    labels = list(dict.fromkeys(all_labels))  # Dedupe, preserve order

    return AggregatedResult(
        is_nsfw=is_nsfw,
        borderline=borderline,
        confidence=round(confidence, 3),
        labels=labels,
        providers_agree=providers_agree,
        providers_total=providers_total,
        provider_results=results,
    )
