from app.config import Settings
from app.costs import estimate_cost, normalize_usage


def test_token_usage_and_configured_cost_estimation():
    settings = Settings(
        model_pricing_json=('{"private-model":{"input_per_million":1.0,"output_per_million":2.0}}')
    )
    usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}

    assert normalize_usage(usage)["input_tokens"] == 1000
    assert estimate_cost(settings, "private-model", usage) == 0.002
    assert estimate_cost(settings, "unknown", usage) is None
