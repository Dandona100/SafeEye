"""Marqo NSFW image detection provider (~22MB model, fast inference)."""
import asyncio
import time
from nsfw_scanner.models import ProviderResult
from nsfw_scanner.providers.base import BaseProvider

_model = None


def _get_model():
    global _model
    if _model is None:
        import timm
        import torch
        _model = timm.create_model("hf_hub:Marqo/nsfw-image-detection-384", pretrained=True)
        _model.eval()
        if torch.cuda.is_available():
            _model = _model.cuda()
    return _model


def _scan_sync(file_path: str) -> dict:
    import torch
    import timm
    from PIL import Image

    model = _get_model()
    device = next(model.parameters()).device

    # Get model-specific transforms
    data_config = timm.data.resolve_model_data_config(model)
    transform = timm.data.create_transform(**data_config, is_training=False)

    img = Image.open(file_path).convert("RGB")
    tensor = transform(img).unsqueeze(0).to(device)

    with torch.no_grad():
        logits = model(tensor)
        probs = torch.softmax(logits, dim=-1)[0]

    # Model outputs: [sfw_score, nsfw_score]
    sfw_score = probs[0].item()
    nsfw_score = probs[1].item()

    is_nsfw = nsfw_score > 0.5
    confidence = nsfw_score if is_nsfw else sfw_score
    label = f"nsfw:{nsfw_score:.2f}" if is_nsfw else f"sfw:{sfw_score:.2f}"

    return {"is_nsfw": is_nsfw, "confidence": confidence, "labels": [label]}


class MarqoNsfwProvider(BaseProvider):
    name = "marqo_nsfw"

    def is_configured(self) -> bool:
        try:
            import timm  # noqa: F401
            import torch  # noqa: F401
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
