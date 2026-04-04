"""Microsoft Azure AI Content Safety provider."""
import os
import time
import base64
import aiohttp
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider


class AzureContentSafetyProvider(BaseProvider):
    name = "azure_content_safety"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("AZURE_CONTENT_SAFETY_KEY") and
            os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT")
        )

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        api_key = os.environ["AZURE_CONTENT_SAFETY_KEY"]
        endpoint = os.environ["AZURE_CONTENT_SAFETY_ENDPOINT"].rstrip("/")
        start = time.monotonic()

        try:
            with open(file_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/contentsafety/image:analyze?api-version=2024-09-01",
                    json={"image": {"content": image_b64}},
                    headers={
                        "Ocp-Apim-Subscription-Key": api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json()

            elapsed = (time.monotonic() - start) * 1000

            if resp.status != 200:
                return ProviderResult(
                    provider=self.name, is_nsfw=False, error=True,
                    labels=[f"api_error:{result}"], latency_ms=round(elapsed, 1),
                )

            labels = []
            max_conf = 0.0

            # Azure returns severity 0-6 for: Hate, SelfHarm, Sexual, Violence
            for cat in result.get("categoriesAnalysis", []):
                category = cat.get("category", "")
                severity = cat.get("severity", 0)
                # severity 0=safe, 2=low, 4=medium, 6=high
                if severity >= 2:
                    conf = severity / 6
                    labels.append(f"{category}:{conf:.2f}")
                    max_conf = max(max_conf, conf)

            return ProviderResult(
                provider=self.name, is_nsfw=len(labels) > 0,
                confidence=max_conf, labels=labels, latency_ms=round(elapsed, 1),
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
