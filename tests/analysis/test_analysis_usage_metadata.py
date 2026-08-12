"""What one alert's analysis cost, recorded on the analysis itself.

The token counts already existed in metrics and in the AI usage table, but both
aggregate — neither can answer "what did THIS alert cost", which is the question
asked while reading one alert.
"""

from services.webhooks.types import ANALYSIS_USAGE, AnalysisResult, set_analysis_usage


def _result() -> AnalysisResult:
    return AnalysisResult(importance="high", summary="disk filling up")


def test_usage_records_model_tokens_and_cost() -> None:
    result = set_analysis_usage(_result(), model="deepseek-v4-pro", tokens_in=3184, tokens_out=611, cost_usd=0.0012743)

    usage = result[ANALYSIS_USAGE]
    assert usage["model"] == "deepseek-v4-pro"
    assert usage["tokens_in"] == 3184
    assert usage["tokens_out"] == 611
    # Rounded, not truncated: sub-cent costs are the normal case here, so
    # six places is the difference between a number and a zero.
    assert usage["cost_usd"] == 0.001274


def test_a_cached_analysis_carries_no_usage() -> None:
    """The load-bearing one. A reuse route pays nothing, so if the cached copy
    kept the original run's tokens, every cache hit would report a purchase
    that never happened — and the dashboard would show a per-alert cost that
    silently double-counts. save_to_cache drops underscore-prefixed keys, which
    is what makes absence mean "this alert cost nothing" rather than "we forgot
    to measure".
    """
    result = set_analysis_usage(_result(), model="m", tokens_in=10, tokens_out=5, cost_usd=0.1)

    # Exactly the filter save_to_cache applies before writing to Redis.
    cached = {k: v for k, v in result.items() if not k.startswith("_")}

    assert ANALYSIS_USAGE not in cached
    assert cached["summary"] == "disk filling up"


def test_usage_survives_a_json_round_trip() -> None:
    """It is stored as JSONB and read back by the dashboard, so the values have
    to be plain JSON — an int-like or float-like that is not actually int/float
    would land as a string on the card."""
    import json

    result = set_analysis_usage(_result(), model="m", tokens_in=1, tokens_out=2, cost_usd=3.5)
    usage = json.loads(json.dumps(result[ANALYSIS_USAGE]))

    assert isinstance(usage["tokens_in"], int)
    assert isinstance(usage["cost_usd"], float)
