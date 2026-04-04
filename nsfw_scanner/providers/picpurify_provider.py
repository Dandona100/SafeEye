"""PicPurify content moderation API - 2,000 free units."""
import os
import time
import aiohttp
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider


class PicPurifyProvider(BaseProvider):
    name = "picpurify"

    def is_configured(self) -> bool:
        return bool(os.environ.get("PICPURIFY_API_KEY"))

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        api_key = os.environ["PICPURIFY_API_KEY"]
        start = time.monotonic()

        try:
            data = aiohttp.FormData()
            data.add_field("file_image", open(file_path, "rb"), filename="scan.jpg")
            data.add_field("API_KEY", api_key)
            data.add_field("task", "porn_moderation,gore_moderation,drug_moderation,weapon_moderation")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://www.picpurify.com/analyse/1.1",
                    data=data, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json()

            elapsed = (time.monotonic() - start) * 1000

            if result.get("status") != "success":
                return ProviderResult(
                    provider=self.name, is_nsfw=False, error=True,
                    labels=[f"api_error:{result.get('error', {}).get('errorMsg', 'unknown')}"],
                    latency_ms=round(elapsed, 1),
                )

            labels = []
            max_conf = 0.0

            for task_name in ["porn_moderation", "gore_moderation", "drug_moderation", "weapon_moderation"]:
                task = result.get(task_name, {})
                conf = task.get("confidence_score", 0)
                is_unsafe = task.get("result", "") != "clean"
                if is_unsafe and conf > 0.4:
                    short = task_name.replace("_moderation", "")
                    labels.append(f"{short}:{conf:.2f}")
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
