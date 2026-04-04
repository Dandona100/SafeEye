"""ModerateContent.com - completely free NSFW detection API."""
import os
import time
import aiohttp
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider


class ModerateContentProvider(BaseProvider):
    name = "moderatecontent"

    def is_configured(self) -> bool:
        return bool(os.environ.get("MODERATECONTENT_API_KEY"))

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        api_key = os.environ["MODERATECONTENT_API_KEY"]
        start = time.monotonic()

        try:
            data = aiohttp.FormData()
            data.add_field("file", open(file_path, "rb"), filename="scan.jpg")
            data.add_field("key", api_key)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.moderatecontent.com/moderate/",
                    data=data, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json()

            elapsed = (time.monotonic() - start) * 1000

            labels = []
            max_conf = 0.0

            # rating_index: 1=safe, 2=soft, 3=adult
            rating = result.get("rating_index", 1)
            rating_label = result.get("rating_label", "safe")

            if rating >= 3:
                conf = float(result.get("predictions", {}).get("adult", 0)) / 100
                labels.append(f"adult:{conf:.2f}")
                max_conf = max(max_conf, conf)
            elif rating >= 2:
                conf = float(result.get("predictions", {}).get("teen", 0)) / 100
                if conf > 0.7:
                    labels.append(f"suggestive:{conf:.2f}")
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
