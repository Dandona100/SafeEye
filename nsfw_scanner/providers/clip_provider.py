"""CLIP zero-shot image classification provider via Hugging Face Inference API."""
import base64
import os
import time

import aiohttp

from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

HF_API_URL = (
    "https://api-inference.huggingface.co/models/openai/clip-vit-base-patch32"
)

# Candidate labels for zero-shot classification.
# The model scores each label against the image; if "safe content" is not the
# highest-scoring label, the image is flagged.
CANDIDATE_LABELS = [
    "safe content",
    "nudity",
    "violence",
    "weapons",
    "drugs",
    "gore",
]

# Labels that we consider unsafe (everything except "safe content").
UNSAFE_LABELS = set(CANDIDATE_LABELS) - {"safe content"}

# Minimum score for an unsafe label to be included in the output.
MIN_LABEL_SCORE = 0.10


class CLIPProvider(BaseProvider):
    name = "clip_search"

    def is_configured(self) -> bool:
        return bool(os.environ.get("HF_API_TOKEN"))

    async def scan(self, file_path: str) -> ProviderResult:
        if not self.is_configured():
            return ProviderResult(provider=self.name, is_nsfw=False, skipped=True)

        token = os.environ["HF_API_TOKEN"]
        start = time.monotonic()

        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            payload = {
                "inputs": {"image": image_b64},
                "parameters": {"candidate_labels": CANDIDATE_LABELS},
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HF_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    elapsed = (time.monotonic() - start) * 1000

                    if resp.status == 503:
                        # Model is loading — treat as temporary error, don't flag.
                        body = await resp.json()
                        return ProviderResult(
                            provider=self.name,
                            is_nsfw=False,
                            error=True,
                            labels=[f"model_loading:{body.get('estimated_time', '?')}s"],
                            latency_ms=round(elapsed, 1),
                        )

                    if resp.status != 200:
                        text = await resp.text()
                        return ProviderResult(
                            provider=self.name,
                            is_nsfw=False,
                            error=True,
                            labels=[f"api_error:{resp.status}:{text[:120]}"],
                            latency_ms=round(elapsed, 1),
                        )

                    result = await resp.json()

            elapsed = (time.monotonic() - start) * 1000

            # HF zero-shot-image-classification returns a list of
            # {"label": str, "score": float} sorted by score descending.
            if not isinstance(result, list) or not result:
                return ProviderResult(
                    provider=self.name,
                    is_nsfw=False,
                    error=True,
                    labels=["unexpected_response_format"],
                    latency_ms=round(elapsed, 1),
                )

            # Build a score map.
            scores = {item["label"]: item["score"] for item in result}
            top_label = result[0]["label"]

            # Collect unsafe labels that scored above MIN_LABEL_SCORE.
            flagged_labels = []
            max_conf = 0.0
            for label in UNSAFE_LABELS:
                score = scores.get(label, 0.0)
                if score >= MIN_LABEL_SCORE:
                    flagged_labels.append(f"{label}:{score:.2f}")
                    max_conf = max(max_conf, score)

            # The image is considered NSFW when the top label is not "safe content".
            is_nsfw = top_label != "safe content" and top_label in UNSAFE_LABELS

            return ProviderResult(
                provider=self.name,
                is_nsfw=is_nsfw,
                confidence=round(max_conf, 3) if is_nsfw else 0.0,
                labels=flagged_labels,
                latency_ms=round(elapsed, 1),
            )

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            return ProviderResult(
                provider=self.name,
                is_nsfw=False,
                error=True,
                labels=[f"error:{e}"],
                latency_ms=round(elapsed, 1),
            )
