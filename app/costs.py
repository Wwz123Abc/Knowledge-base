from __future__ import annotations

from app.config import Settings


def normalize_usage(usage: dict) -> dict[str, int]:
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def estimate_cost(settings: Settings, model: str, usage: dict) -> float | None:
    pricing = settings.model_pricing.get(model)
    if not pricing:
        return None
    normalized = normalize_usage(usage)
    input_cost = normalized["input_tokens"] * float(pricing.get("input_per_million", 0))
    output_cost = normalized["output_tokens"] * float(pricing.get("output_per_million", 0))
    return round((input_cost + output_cost) / 1_000_000, 8)
