"""Amazon Rekognition content moderation provider."""
import os
import asyncio
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider


def _scan_sync(file_path: str) -> dict:
    import boto3
    client = boto3.client(
        "rekognition",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    with open(file_path, "rb") as f:
        image_bytes = f.read()

    response = client.detect_moderation_labels(
        Image={"Bytes": image_bytes},
        MinConfidence=40,
    )

    labels = []
    max_conf = 0.0

    for label in response.get("ModerationLabels", []):
        name = label.get("Name", "")
        conf = label.get("Confidence", 0) / 100
        category = label.get("ParentName", "")

        if conf >= 0.4:
            tag = f"{category}/{name}" if category else name
            labels.append(f"{tag}:{conf:.2f}")
            max_conf = max(max_conf, conf)

    return {"is_nsfw": len(labels) > 0, "confidence": max_conf, "labels": labels}


class AmazonRekognitionProvider(BaseProvider):
    name = "amazon_rekognition"

    def is_configured(self) -> bool:
        try:
            import boto3  # noqa: F401
            return bool(
                os.environ.get("AWS_ACCESS_KEY_ID") and
                os.environ.get("AWS_SECRET_ACCESS_KEY")
            )
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
                provider=self.name, is_nsfw=result["is_nsfw"],
                confidence=result["confidence"], labels=result["labels"],
                latency_ms=round(elapsed, 1),
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
