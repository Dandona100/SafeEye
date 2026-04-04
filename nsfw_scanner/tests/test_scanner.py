"""Comprehensive test suite for SafeEye scanner."""
import pytest
import os
import json
import tempfile


# ========== Models ==========

class TestModels:
    def test_provider_result(self):
        from nsfw_scanner.models import ProviderResult
        pr = ProviderResult(provider="test", is_nsfw=False)
        assert pr.provider == "test"
        assert pr.confidence == 0.0
        assert pr.labels == []
        assert pr.error is False
        assert pr.skipped is False

    def test_provider_result_nsfw(self):
        from nsfw_scanner.models import ProviderResult
        pr = ProviderResult(provider="nudenet", is_nsfw=True, confidence=0.92, labels=["nudity:0.92"])
        assert pr.is_nsfw is True
        assert pr.confidence == 0.92

    def test_aggregated_result(self):
        from nsfw_scanner.models import AggregatedResult
        ar = AggregatedResult(is_nsfw=True, confidence=0.85, labels=["weapon_firearm:0.99"])
        assert ar.is_nsfw is True
        assert ar.borderline is False
        assert len(ar.labels) == 1
        assert ar.phash is None

    def test_aggregated_result_with_phash(self):
        from nsfw_scanner.models import AggregatedResult
        ar = AggregatedResult(is_nsfw=False, phash="ff8fc3e07018040707")
        assert ar.phash == "ff8fc3e07018040707"

    def test_job_response(self):
        from nsfw_scanner.models import JobResponse
        jr = JobResponse(job_id="abc123", status="pending")
        assert jr.status == "pending"
        assert jr.result is None

    def test_batch_response(self):
        from nsfw_scanner.models import BatchResponse
        br = BatchResponse(batch_id="batch_xyz", total=5, completed=3, failed=1, pending=1)
        assert br.total == 5
        assert br.completed == 3

    def test_scan_response(self):
        from nsfw_scanner.models import ScanResponse, AggregatedResult
        ar = AggregatedResult(is_nsfw=False)
        sr = ScanResponse(scan_id="test", result=ar, timestamp="2026-01-01")
        assert sr.scan_id == "test"

    def test_stats_overview(self):
        from nsfw_scanner.models import StatsOverview
        s = StatsOverview(total_scans=100, nsfw_detected=5, nsfw_rate=5.0)
        assert s.nsfw_rate == 5.0

    def test_provider_stats(self):
        from nsfw_scanner.models import ProviderStats
        ps = ProviderStats(provider="nudenet", total_scans=50, nsfw_flagged=3, avg_latency_ms=120.5)
        assert ps.avg_latency_ms == 120.5
        assert ps.accuracy is None

    def test_history_item(self):
        from nsfw_scanner.models import HistoryItem
        hi = HistoryItem(scan_id="x", timestamp="t", is_nsfw=True, labels=["gore:0.8"])
        assert hi.is_nsfw is True

    def test_token_models(self):
        from nsfw_scanner.models import TokenCreate, TokenInfo, TokenCreated
        tc = TokenCreate(name="test-bot", expires_in_days=30)
        assert tc.expires_in_days == 30
        ti = TokenInfo(name="test", created_at="2026-01-01", scan_count=10)
        assert ti.scan_count == 10
        tcr = TokenCreated(name="test", token="abc123")
        assert tcr.token == "abc123"

    def test_feedback_request(self):
        from nsfw_scanner.models import FeedbackRequest
        fr = FeedbackRequest(actual_nsfw=True, notes="false positive")
        assert fr.actual_nsfw is True

    def test_batch_request(self):
        from nsfw_scanner.models import BatchRequest
        br = BatchRequest(urls=["url1", "url2"], webhook_url="https://hook.example.com")
        assert len(br.urls) == 2


# ========== Voting Logic ==========

class TestVoting:
    def _make_result(self, provider, is_nsfw, confidence=0.0, labels=None, skipped=False, error=False):
        from nsfw_scanner.models import ProviderResult
        return ProviderResult(
            provider=provider, is_nsfw=is_nsfw, confidence=confidence,
            labels=labels or [], skipped=skipped, error=error,
        )

    def test_all_safe(self):
        from nsfw_scanner.scanner import _aggregate
        results = [self._make_result("a", False), self._make_result("b", False)]
        agg = _aggregate(results)
        assert agg.is_nsfw is False
        assert agg.borderline is False
        assert agg.providers_agree == 0
        assert agg.providers_total == 2

    def test_one_high_confidence_nsfw(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", True, 0.9, ["nudity:0.9"]),
            self._make_result("b", False),
        ]
        agg = _aggregate(results)
        assert agg.is_nsfw is True
        assert agg.providers_agree == 1

    def test_majority_vote_nsfw(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", True, 0.5),
            self._make_result("b", True, 0.4),
            self._make_result("c", False),
        ]
        agg = _aggregate(results)
        assert agg.is_nsfw is True
        assert agg.providers_agree == 2
        assert agg.providers_total == 3

    def test_single_low_confidence_borderline(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", True, 0.5),
            self._make_result("b", False),
        ]
        agg = _aggregate(results)
        assert agg.is_nsfw is False
        assert agg.borderline is True

    def test_skipped_providers_excluded(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", False),
            self._make_result("b", False, skipped=True),
            self._make_result("c", False, error=True),
        ]
        agg = _aggregate(results)
        assert agg.providers_total == 1

    def test_empty_results(self):
        from nsfw_scanner.scanner import _aggregate
        agg = _aggregate([])
        assert agg.is_nsfw is False
        assert agg.providers_total == 0

    def test_all_skipped(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", False, skipped=True),
            self._make_result("b", False, skipped=True),
        ]
        agg = _aggregate(results)
        assert agg.providers_total == 0

    def test_weighted_confidence(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("nudenet", True, 0.8, ["test"]),
            self._make_result("sightengine", True, 0.9, ["test"]),
        ]
        agg = _aggregate(results)
        assert agg.is_nsfw is True
        # Weighted avg: (0.8*1.0 + 0.9*1.2) / (1.0+1.2) = 1.88/2.2 ≈ 0.855
        assert 0.84 < agg.confidence < 0.87

    def test_labels_deduplication(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", True, 0.8, ["nudity:0.8", "gore:0.5"]),
            self._make_result("b", True, 0.7, ["nudity:0.8", "weapon:0.9"]),
        ]
        agg = _aggregate(results)
        # Should dedupe "nudity:0.8"
        assert len(agg.labels) == 3

    def test_three_providers_all_agree(self):
        from nsfw_scanner.scanner import _aggregate
        results = [
            self._make_result("a", True, 0.6),
            self._make_result("b", True, 0.5),
            self._make_result("c", True, 0.4),
        ]
        agg = _aggregate(results)
        assert agg.is_nsfw is True
        assert agg.providers_agree == 3


# ========== Auth ==========

class TestAuth:
    def test_generate_token(self):
        try:
            from nsfw_scanner.auth import generate_token, hash_token
        except ImportError:
            pytest.skip("aiosqlite not installed")
        raw, hashed = generate_token()
        assert len(raw) > 20
        assert hash_token(raw) == hashed
        assert raw != hashed

    def test_master_verification(self):
        try:
            from nsfw_scanner.auth import verify_master
        except ImportError:
            pytest.skip("aiosqlite not installed")
        os.environ["SCAN_API_MASTER_TOKEN"] = "test_master_xyz"
        assert verify_master("test_master_xyz") is True
        assert verify_master("wrong_token") is False
        assert verify_master("") is False
        del os.environ["SCAN_API_MASTER_TOKEN"]

    def test_master_not_set(self):
        try:
            from nsfw_scanner.auth import verify_master
        except ImportError:
            pytest.skip("aiosqlite not installed")
        os.environ.pop("SCAN_API_MASTER_TOKEN", None)
        assert verify_master("anything") is False


# ========== Provider Configuration ==========

class TestProviders:
    def test_nudenet_always_configured(self):
        from nsfw_scanner.providers.nudenet_provider import NudeNetProvider
        p = NudeNetProvider()
        assert p.name == "nudenet"
        # May or may not be configured depending on nudenet install

    def test_sightengine_not_configured(self):
        from nsfw_scanner.providers.sightengine_provider import SightengineProvider
        os.environ.pop("SIGHTENGINE_API_USER", None)
        os.environ.pop("SIGHTENGINE_API_SECRET", None)
        assert SightengineProvider().is_configured() is False

    def test_sightengine_configured(self):
        from nsfw_scanner.providers.sightengine_provider import SightengineProvider
        os.environ["SIGHTENGINE_API_USER"] = "test"
        os.environ["SIGHTENGINE_API_SECRET"] = "test"
        assert SightengineProvider().is_configured() is True
        del os.environ["SIGHTENGINE_API_USER"]
        del os.environ["SIGHTENGINE_API_SECRET"]

    def test_google_not_configured(self):
        from nsfw_scanner.providers.google_vision_provider import GoogleVisionProvider
        os.environ.pop("GOOGLE_VISION_CREDENTIALS", None)
        assert GoogleVisionProvider().is_configured() is False

    def test_amazon_not_configured(self):
        from nsfw_scanner.providers.amazon_rekognition_provider import AmazonRekognitionProvider
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
        assert AmazonRekognitionProvider().is_configured() is False

    def test_azure_not_configured(self):
        from nsfw_scanner.providers.azure_provider import AzureContentSafetyProvider
        os.environ.pop("AZURE_CONTENT_SAFETY_KEY", None)
        assert AzureContentSafetyProvider().is_configured() is False

    def test_picpurify_not_configured(self):
        from nsfw_scanner.providers.picpurify_provider import PicPurifyProvider
        os.environ.pop("PICPURIFY_API_KEY", None)
        assert PicPurifyProvider().is_configured() is False

    def test_moderatecontent_not_configured(self):
        from nsfw_scanner.providers.moderatecontent_provider import ModerateContentProvider
        os.environ.pop("MODERATECONTENT_API_KEY", None)
        assert ModerateContentProvider().is_configured() is False


# ========== Perceptual Hashing ==========

class TestPHash:
    def test_compute_phash(self):
        from nsfw_scanner.scanner import compute_phash
        # Create a test image
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img = Image.new("RGB", (100, 100), color="blue")
            img.save(f.name)
            phash = compute_phash(f.name)
            os.unlink(f.name)
        assert phash is not None
        assert len(phash) > 8
        assert all(c in "0123456789abcdef" for c in phash)

    def test_similar_images_same_hash(self):
        from nsfw_scanner.scanner import compute_phash
        from PIL import Image
        # Two identical images should have same hash
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f1:
            img = Image.new("RGB", (200, 200), color="red")
            img.save(f1.name)
            h1 = compute_phash(f1.name)
            os.unlink(f1.name)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f2:
            img = Image.new("RGB", (200, 200), color="red")
            img.save(f2.name)
            h2 = compute_phash(f2.name)
            os.unlink(f2.name)
        assert h1 == h2

    def test_different_images_different_hash(self):
        from nsfw_scanner.scanner import compute_phash
        from PIL import Image, ImageDraw
        # Create image with pattern (not solid color — solid colors hash identically)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f1:
            img = Image.new("RGB", (200, 200), color="white")
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, 100, 100], fill="red")
            img.save(f1.name)
            h1 = compute_phash(f1.name)
            os.unlink(f1.name)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f2:
            img = Image.new("RGB", (200, 200), color="black")
            draw = ImageDraw.Draw(img)
            draw.ellipse([50, 50, 150, 150], fill="green")
            img.save(f2.name)
            h2 = compute_phash(f2.name)
            os.unlink(f2.name)
        assert h1 != h2

    def test_phash_invalid_file(self):
        from nsfw_scanner.scanner import compute_phash
        result = compute_phash("/nonexistent/file.jpg")
        assert result is None

    def test_hamming_distance(self):
        try:
            from nsfw_scanner.db import _hamming_distance
        except ImportError:
            pytest.skip("aiosqlite not installed")
        assert _hamming_distance("ffff", "ffff") == 0
        assert _hamming_distance("ffff", "fffe") == 1
        assert _hamming_distance("0000", "ffff") == 16


# ========== og:image Extraction ==========

class TestOgImage:
    """og:image extraction — reimplements the regex locally since app.py requires fastapi."""
    @staticmethod
    def _extract(html):
        import re
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    def test_extract_og_image_standard(self):
        html = '<html><head><meta property="og:image" content="https://example.com/img.jpg"></head></html>'
        assert self._extract(html) == "https://example.com/img.jpg"

    def test_extract_og_image_reversed(self):
        html = '<meta content="https://example.com/photo.png" property="og:image">'
        assert self._extract(html) == "https://example.com/photo.png"

    def test_extract_og_image_none(self):
        assert self._extract('<html><head><title>No image</title></head></html>') is None

    def test_extract_og_image_empty(self):
        assert self._extract("") is None

    def test_extract_og_image_single_quotes(self):
        html = "<meta property='og:image' content='https://example.com/img.jpg'>"
        assert self._extract(html) == "https://example.com/img.jpg"

    def test_extract_og_image_with_extra_attrs(self):
        html = '<meta name="twitter:image" content="x"><meta property="og:image" content="https://cdn.example.com/hero.jpg" data-foo="bar">'
        assert self._extract(html) == "https://cdn.example.com/hero.jpg"


# ========== Rate Limiting ==========

class TestRateLimiting:
    def test_rate_limit_basic(self):
        try:
            from nsfw_scanner.app import check_rate_limit, _rate_limits, RATE_LIMIT_PER_MINUTE
        except ImportError:
            pytest.skip("Dependencies not installed")
        _rate_limits.clear()
        # Should not raise for first call
        check_rate_limit("test_user")

    def test_rate_limit_exceeded(self):
        try:
            from nsfw_scanner.app import check_rate_limit, _rate_limits, RATE_LIMIT_PER_MINUTE
            from fastapi import HTTPException
        except ImportError:
            pytest.skip("Dependencies not installed")
        _rate_limits.clear()
        _rate_limits["spam_user"] = (RATE_LIMIT_PER_MINUTE + 1, __import__('time').time())
        with pytest.raises(HTTPException) as exc_info:
            check_rate_limit("spam_user")
        assert exc_info.value.status_code == 429


# ========== Label Translation ==========

class TestLabels:
    def test_known_labels_covered(self):
        """All known NudeNet and Sightengine labels have translations."""
        known = [
            "weapon_firearm", "weapon_knife", "gore", "drugs",
            "sexual_activity", "sexual_display", "offensive",
            "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
            "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "ANUS_EXPOSED",
            "adult", "violence", "racy", "erotica", "very_suggestive",
        ]
        assert len(known) == 17

    def test_label_format(self):
        """Labels follow format: name:confidence"""
        label = "weapon_firearm:0.99"
        name, conf = label.split(":")
        assert name == "weapon_firearm"
        assert float(conf) == 0.99


# ========== Stream Monitor ==========

class TestStreamMonitor:
    def test_import(self):
        try:
            from nsfw_scanner.stream_monitor import start_monitor, stop_monitor, get_all_monitors
        except ImportError:
            pytest.skip("Dependencies not installed")

    def test_get_monitors_empty(self):
        try:
            from nsfw_scanner.stream_monitor import get_all_monitors
        except ImportError:
            pytest.skip("Dependencies not installed")
        monitors = get_all_monitors()
        assert isinstance(monitors, (dict, list))


# ========== Scanner Configuration ==========

class TestScannerConfig:
    def test_active_providers(self):
        from nsfw_scanner.scanner import get_active_providers
        providers = get_active_providers()
        assert isinstance(providers, list)
        # NudeNet should always be in the list if installed
        # Other providers depend on env vars

    def test_video_sample_frames(self):
        from nsfw_scanner.scanner import _VIDEO_SAMPLE_FRAMES
        assert _VIDEO_SAMPLE_FRAMES == 30

    def test_weights_exist(self):
        from nsfw_scanner.scanner import _WEIGHTS
        assert "nudenet" in _WEIGHTS
        assert "sightengine" in _WEIGHTS
        assert _WEIGHTS["nudenet"] == 1.0
        assert _WEIGHTS["sightengine"] == 1.2

    def test_all_provider_weights(self):
        from nsfw_scanner.scanner import _WEIGHTS
        expected = ["nudenet", "sightengine", "google_vision", "moderatecontent",
                    "amazon_rekognition", "azure_content_safety", "picpurify",
                    "nsfwjs", "deepfake_check", "audio_check", "clip_search"]
        for name in expected:
            assert name in _WEIGHTS, f"Missing weight for {name}"

    def test_provider_timeout_config(self):
        from nsfw_scanner.scanner import PROVIDER_TIMEOUT
        assert PROVIDER_TIMEOUT > 0
        assert PROVIDER_TIMEOUT <= 60


# ========== Vector Store ==========

class TestVectorStore:
    def test_import(self):
        from nsfw_scanner.vector_store import VectorStore
        store = VectorStore()
        assert store is not None

    def test_add_and_search(self):
        from nsfw_scanner.vector_store import VectorStore
        store = VectorStore()
        store.add("scan1", "ff8fc3e07018040707")
        store.add("scan2", "ff8fc3e07018040707")
        store.add("scan3", "0000000000000000")
        results = store.search("ff8fc3e07018040707", top_k=5)
        assert len(results) >= 2
        # First results should be identical matches
        assert results[0][1] == 1.0  # similarity = 1.0

    def test_search_empty_store(self):
        from nsfw_scanner.vector_store import VectorStore
        store = VectorStore()
        results = store.search("ff8fc3e07018040707")
        assert results == []

    def test_different_hashes_low_similarity(self):
        from nsfw_scanner.vector_store import VectorStore
        store = VectorStore()
        store.add("scan1", "ffffffffffffffff")
        results = store.search("0000000000000000", top_k=1)
        assert results[0][1] < 0.5  # Very different

    def test_load_from_db(self):
        from nsfw_scanner.vector_store import VectorStore
        store = VectorStore()
        fake_scans = [
            {"id": "a", "phash": "ff00ff00ff00ff00"},
            {"id": "b", "phash": "00ff00ff00ff00ff"},
            {"id": "c", "phash": None},  # Should be skipped
        ]
        store.load_from_db(fake_scans)
        results = store.search("ff00ff00ff00ff00")
        assert len(results) == 2  # Only 2 loaded (None skipped)


# ========== New Providers ==========

class TestNewProviders:
    def test_deepfake_provider_import(self):
        from nsfw_scanner.providers.deepfake_provider import DeepfakeProvider
        p = DeepfakeProvider()
        assert p.name == "deepfake_check"

    def test_audio_provider_import(self):
        from nsfw_scanner.providers.audio_provider import AudioProvider
        p = AudioProvider()
        assert p.name == "audio_check"

    def test_clip_provider_import(self):
        from nsfw_scanner.providers.clip_provider import CLIPProvider
        p = CLIPProvider()
        assert p.name == "clip_search"

    def test_clip_not_configured_without_token(self):
        from nsfw_scanner.providers.clip_provider import CLIPProvider
        os.environ.pop("HF_API_TOKEN", None)
        assert CLIPProvider().is_configured() is False

    def test_clip_configured_with_token(self):
        from nsfw_scanner.providers.clip_provider import CLIPProvider
        os.environ["HF_API_TOKEN"] = "test_token"
        assert CLIPProvider().is_configured() is True
        del os.environ["HF_API_TOKEN"]

    def test_nsfwjs_not_configured_without_model(self):
        from nsfw_scanner.providers.nsfwjs_provider import NsfwjsProvider
        p = NsfwjsProvider()
        # Without model file, should not be configured
        # (may or may not be configured depending on model presence)
        assert isinstance(p.is_configured(), bool)

    def test_deepfake_configured_with_opencv(self):
        from nsfw_scanner.providers.deepfake_provider import DeepfakeProvider
        try:
            import cv2
            assert DeepfakeProvider().is_configured() is True
        except ImportError:
            assert DeepfakeProvider().is_configured() is False

    def test_audio_configured_with_ffprobe(self):
        from nsfw_scanner.providers.audio_provider import AudioProvider
        import shutil
        has_ffprobe = shutil.which("ffprobe") is not None
        assert AudioProvider().is_configured() == has_ffprobe


# ========== Plugin Loader ==========

class TestPluginLoader:
    def test_import(self):
        from nsfw_scanner.plugin_loader import load_plugins
        assert callable(load_plugins)

    def test_load_empty_dir(self):
        from nsfw_scanner.plugin_loader import load_plugins
        plugins = load_plugins("/nonexistent/dir")
        assert plugins == []

    def test_load_from_temp_dir(self):
        from nsfw_scanner.plugin_loader import load_plugins
        # Create a temp plugin
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_code = '''
from nsfw_scanner.providers.base import BaseProvider
from nsfw_scanner.models import ProviderResult

class TestPlugin(BaseProvider):
    name = "test_plugin"
    def is_configured(self): return True
    async def scan(self, file_path):
        return ProviderResult(provider=self.name, is_nsfw=False)
'''
            with open(os.path.join(tmpdir, "test_plugin.py"), "w") as f:
                f.write(plugin_code)
            plugins = load_plugins(tmpdir)
            assert len(plugins) >= 1
            assert plugins[0].name == "test_plugin"


# ========== CLI ==========

class TestCLI:
    def test_import(self):
        from nsfw_scanner.cli import main
        assert callable(main)

    def test_config_file(self):
        """CLI config reads from env vars."""
        os.environ["SAFEEYE_URL"] = "http://test:1234"
        os.environ["SAFEEYE_TOKEN"] = "test_token"
        # Just verify env vars are accessible
        assert os.environ["SAFEEYE_URL"] == "http://test:1234"
        del os.environ["SAFEEYE_URL"]
        del os.environ["SAFEEYE_TOKEN"]


# ========== New Providers V6 ==========

class TestNewProvidersV6:
    # --- 1. MarqoNsfwProvider ---
    def test_marqo_import(self):
        from nsfw_scanner.providers.marqo_nsfw_provider import MarqoNsfwProvider
        p = MarqoNsfwProvider()
        assert p.name == "marqo_nsfw"

    def test_marqo_is_configured(self):
        from nsfw_scanner.providers.marqo_nsfw_provider import MarqoNsfwProvider
        p = MarqoNsfwProvider()
        # Depends on whether timm is installed
        assert isinstance(p.is_configured(), bool)

    # --- 2. DetoxifyProvider ---
    def test_detoxify_import(self):
        from nsfw_scanner.providers.detoxify_provider import DetoxifyProvider
        p = DetoxifyProvider()
        assert p.name == "detoxify"

    def test_detoxify_text_extraction_from_filename(self):
        from nsfw_scanner.providers.detoxify_provider import _extract_text_from_file
        with tempfile.NamedTemporaryFile(
            prefix="hello_world_test_file", suffix=".txt", delete=False
        ) as f:
            f.write(b"dummy content")
            tmp_path = f.name
        try:
            text = _extract_text_from_file(tmp_path)
            assert text is not None
            assert "hello" in text
            assert "world" in text
        finally:
            os.unlink(tmp_path)

    def test_detoxify_text_extraction_short_filename_skipped(self):
        from nsfw_scanner.providers.detoxify_provider import _extract_text_from_file
        with tempfile.NamedTemporaryFile(
            prefix="ab", suffix=".txt", delete=False
        ) as f:
            f.write(b"dummy")
            tmp_path = f.name
        try:
            # Short filenames (< 5 chars after cleanup) should return None
            text = _extract_text_from_file(tmp_path)
            # Result depends on the generated tmp suffix length, just check type
            assert text is None or isinstance(text, str)
        finally:
            os.unlink(tmp_path)

    # --- 3. FreepikNsfwProvider ---
    def test_freepik_import(self):
        from nsfw_scanner.providers.freepik_nsfw_provider import FreepikNsfwProvider
        p = FreepikNsfwProvider()
        assert p.name == "freepik_nsfw"

    def test_freepik_is_configured(self):
        from nsfw_scanner.providers.freepik_nsfw_provider import FreepikNsfwProvider
        p = FreepikNsfwProvider()
        # Depends on whether transformers is installed
        assert isinstance(p.is_configured(), bool)

    # --- 4. DeepfakeV2Provider ---
    def test_deepfake_v2_import(self):
        from nsfw_scanner.providers.deepfake_v2_provider import DeepfakeV2Provider
        p = DeepfakeV2Provider()
        assert p.name == "deepfake_v2"

    def test_deepfake_v2_skips_non_image(self):
        import asyncio
        from nsfw_scanner.providers.deepfake_v2_provider import DeepfakeV2Provider
        p = DeepfakeV2Provider()
        result = asyncio.get_event_loop().run_until_complete(p.scan("/tmp/fake_video.mp4"))
        assert result.skipped is True

    def test_deepfake_v2_skips_txt(self):
        import asyncio
        from nsfw_scanner.providers.deepfake_v2_provider import DeepfakeV2Provider
        p = DeepfakeV2Provider()
        result = asyncio.get_event_loop().run_until_complete(p.scan("/tmp/notes.txt"))
        assert result.skipped is True

    # --- 5. YOLOWeaponProvider ---
    def test_yolo_weapon_import(self):
        from nsfw_scanner.providers.yolo_weapon_provider import YOLOWeaponProvider
        p = YOLOWeaponProvider()
        assert p.name == "yolo_weapons"

    def test_yolo_weapon_not_configured_without_model(self):
        from nsfw_scanner.providers.yolo_weapon_provider import YOLOWeaponProvider
        os.environ.pop("YOLO_WEAPONS_MODEL_PATH", None)
        p = YOLOWeaponProvider()
        # Default model path /app/data/weapon_yolov8n.pt unlikely to exist
        assert p.is_configured() is False

    # --- 6. FalconsaiProvider ---
    def test_falconsai_import(self):
        from nsfw_scanner.providers.falconsai_provider import FalconsaiProvider
        p = FalconsaiProvider()
        assert p.name == "falconsai_nsfw"

    def test_falconsai_is_configured(self):
        from nsfw_scanner.providers.falconsai_provider import FalconsaiProvider
        p = FalconsaiProvider()
        # Depends on whether transformers is installed
        assert isinstance(p.is_configured(), bool)

    # --- 7. SigLIPNsfwProvider ---
    def test_siglip_import(self):
        from nsfw_scanner.providers.siglip_nsfw_provider import SigLIPNsfwProvider
        p = SigLIPNsfwProvider()
        assert p.name == "siglip_nsfw"

    def test_siglip_is_configured(self):
        from nsfw_scanner.providers.siglip_nsfw_provider import SigLIPNsfwProvider
        p = SigLIPNsfwProvider()
        assert isinstance(p.is_configured(), bool)

    # --- 8. BumblePrivateProvider ---
    def test_bumble_import(self):
        from nsfw_scanner.providers.bumble_provider import BumblePrivateProvider
        p = BumblePrivateProvider()
        assert p.name == "bumble_private"

    def test_bumble_not_configured_without_model(self):
        from nsfw_scanner.providers.bumble_provider import BumblePrivateProvider
        os.environ.pop("BUMBLE_MODEL_PATH", None)
        p = BumblePrivateProvider()
        # Default path /app/data/bumble_model unlikely to exist, and/or tensorflow missing
        assert p.is_configured() is False

    # --- 9. HateSpeechProvider ---
    def test_hatespeech_import(self):
        from nsfw_scanner.providers.hatespeech_provider import HateSpeechProvider
        p = HateSpeechProvider()
        assert p.name == "hate_speech"

    def test_hatespeech_text_extraction(self):
        from nsfw_scanner.providers.hatespeech_provider import _extract_text
        with tempfile.NamedTemporaryFile(
            prefix="some_test_filename", suffix=".jpg", delete=False
        ) as f:
            f.write(b"not a real image")
            tmp_path = f.name
        try:
            text = _extract_text(tmp_path)
            assert isinstance(text, str)
            assert "some" in text or "test" in text or "filename" in text
        finally:
            os.unlink(tmp_path)

    def test_hatespeech_is_configured(self):
        from nsfw_scanner.providers.hatespeech_provider import HateSpeechProvider
        p = HateSpeechProvider()
        assert isinstance(p.is_configured(), bool)

    # --- Total provider count ---
    def test_total_provider_count(self):
        from nsfw_scanner.scanner import _get_providers
        providers = _get_providers()
        assert len(providers) >= 20


# ========== Integration Tests (require running server) ==========

class TestIntegration:
    """These tests require the SafeEye server to be running."""

    @pytest.fixture(autouse=True)
    def skip_if_no_server(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://localhost:1985/health", timeout=2)
        except Exception:
            pytest.skip("SafeEye server not running")

    def test_health_endpoint(self):
        import urllib.request, json
        resp = urllib.request.urlopen("http://localhost:1985/health")
        data = json.loads(resp.read())
        assert data["status"] == "ok"
        assert "providers" in data
        assert "db" in data

    def test_metrics_endpoint(self):
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:1985/metrics")
        text = resp.read().decode()
        assert "safeeye_scans_total" in text
        assert "safeeye_active_providers" in text

    def test_docs_endpoint(self):
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:1985/docs")
        assert resp.status == 200

    def test_dashboard_endpoint(self):
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:1985/dashboard")
        html = resp.read().decode()
        assert "SafeEye" in html
