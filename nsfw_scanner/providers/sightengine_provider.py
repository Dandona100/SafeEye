"""Sightengine commercial API provider."""
import os
import time
import aiohttp
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider


class SightengineProvider(BaseProvider):
    name = "sightengine"

    def is_configured(self) -> bool:
        return bool(os.environ.get("SIGHTENGINE_API_USER") and os.environ.get("SIGHTENGINE_API_SECRET"))

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        api_user = os.environ["SIGHTENGINE_API_USER"]
        api_secret = os.environ["SIGHTENGINE_API_SECRET"]
        start = time.monotonic()

        try:
            data = aiohttp.FormData()
            data.add_field("media", open(file_path, "rb"), filename="scan.jpg")
            data.add_field("models", "nudity-2.1,gore-2.0,recreational_drug,weapon,offensive")
            data.add_field("api_user", api_user)
            data.add_field("api_secret", api_secret)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.sightengine.com/1.0/check.json",
                    data=data, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    result = await resp.json()

            elapsed = (time.monotonic() - start) * 1000

            if result.get("status") != "success":
                return ProviderResult(
                    provider=self.name, is_nsfw=False, error=True,
                    labels=[f"api_error:{result.get('error', {}).get('message', 'unknown')}"],
                    latency_ms=round(elapsed, 1),
                )

            labels = []
            max_conf = 0.0

            nudity = result.get("nudity", {})
            for key, threshold in [
                ("sexual_activity", 0.4), ("sexual_display", 0.4),
                ("erotica", 0.6), ("very_suggestive", 0.7),
            ]:
                val = nudity.get(key, 0)
                if val and val > threshold:
                    labels.append(f"{key}:{val:.2f}")
                    max_conf = max(max_conf, val)

            # Helper: extract probability from Sightengine response
            # Fields can be float (0.95) or dict ({"prob": 0.95, "classes": {...}})
            def _prob(val):
                if isinstance(val, dict):
                    return val.get("prob", 0) or 0
                return val if isinstance(val, (int, float)) else 0

            gore_prob = _prob(result.get("gore", 0))
            if gore_prob > 0.5:
                labels.append(f"gore:{gore_prob:.2f}")
                max_conf = max(max_conf, gore_prob)

            drugs = _prob(result.get("recreational_drug", 0)) or _prob(result.get("drugs", 0))
            if drugs > 0.5:
                labels.append(f"drugs:{drugs:.2f}")
                max_conf = max(max_conf, drugs)

            weapon_data = result.get("weapon", {})
            if isinstance(weapon_data, dict):
                classes = weapon_data.get("classes", {})
                for wtype in ["firearm", "knife"]:
                    wval = classes.get(wtype, 0) or 0
                    if wval > 0.3:
                        labels.append(f"weapon_{wtype}:{wval:.2f}")
                        max_conf = max(max_conf, wval)
            else:
                weapon = _prob(weapon_data)
                if weapon > 0.3:
                    labels.append(f"weapon:{weapon:.2f}")
                    max_conf = max(max_conf, weapon)

            offensive = _prob(result.get("offensive", 0))
            if offensive > 0.7:
                labels.append(f"offensive:{offensive:.2f}")
                max_conf = max(max_conf, offensive)

            return ProviderResult(
                provider=self.name,
                is_nsfw=len(labels) > 0,
                confidence=max_conf,
                labels=labels,
                latency_ms=round(elapsed, 1),
            )

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name, is_nsfw=False, error=True,
                labels=[f"error:{e}"], latency_ms=round(elapsed, 1),
            )
