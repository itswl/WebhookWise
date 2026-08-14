"""Tests for tiered AI routing (#B): low-value alerts skip the paid LLM."""

from unittest.mock import AsyncMock

import pytest


def _low_alert() -> dict[str, object]:
    # Level "info" → analyze_with_rules classifies as low importance.
    return {"source": "prometheus", "parsed_data": {"RuleName": "DiskUsageOK", "Level": "info"}}


def _high_alert() -> dict[str, object]:
    return {"source": "prometheus", "parsed_data": {"RuleName": "ServiceDownCritical", "Level": "critical"}}


def _stub_common(monkeypatch, temp_config, llm_spy) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", AsyncMock())
    # Spy on the LLM call so we can assert whether it ran.
    monkeypatch.setattr(ai_analyzer._llm_client, "call_ai_with_breaker", llm_spy)


@pytest.mark.asyncio
async def test_routing_enabled_skips_llm_for_low_value_alert(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high"}, 1, 1))
    _stub_common(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_SKIP_IMPORTANCE", "low")

    result = await ai_analyzer.analyze_webhook_with_ai(_low_alert(), alert_hash="h-low", skip_cache=True)

    assert result["_route_type"] == "rule_routed"
    assert result.get("_degraded") is not True  # routing is intentional, not a degradation
    llm_spy.assert_not_awaited()  # LLM (paid) skipped


@pytest.mark.asyncio
async def test_routing_disabled_still_calls_llm_for_low_alert(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "low", "summary": "ai"}, 5, 5))
    _stub_common(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", False)  # default

    result = await ai_analyzer.analyze_webhook_with_ai(_low_alert(), alert_hash="h-low2", skip_cache=True)

    assert result["_route_type"] == "ai"
    llm_spy.assert_awaited_once()  # behavior unchanged when routing off


@pytest.mark.asyncio
async def test_routing_enabled_still_calls_llm_for_high_alert(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "ai"}, 5, 5))
    _stub_common(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_SKIP_IMPORTANCE", "low")

    result = await ai_analyzer.analyze_webhook_with_ai(_high_alert(), alert_hash="h-high", skip_cache=True)

    assert result["_route_type"] == "ai"
    llm_spy.assert_awaited_once()  # high-importance is NOT in the skip set


def _info_payment_alert() -> dict[str, object]:
    """What production actually sent: money, labelled `info`, still firing.

    19 of these were filed low by the rule pass and, with tiered routing on,
    never reached the model — which called every payment alert it did see
    "high".
    """
    return {
        "source": "grafana",
        "parsed_data": {
            "RuleName": "示例充值超限告警",
            "Level": "info",
            "status": "firing",
            "title": "[FIRING:1] 示例充值超限告警",
            "message": "当前值为 920.00，超过阈值 500",
        },
    }


@pytest.mark.asyncio
async def test_a_firing_money_alert_labelled_info_still_reaches_the_model(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "money"}, 1, 1))
    _stub_common(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_SKIP_IMPORTANCE", "low")

    result = await ai_analyzer.analyze_webhook_with_ai(_info_payment_alert(), alert_hash="h-pay", skip_cache=True)

    assert result["_route_type"] != "rule_routed"
    llm_spy.assert_awaited()


@pytest.mark.asyncio
async def test_the_recovery_of_that_same_alert_is_still_free(monkeypatch, temp_config) -> None:
    """The floor must not turn every recovery notice into a paid call.

    Once the condition is over, how bad it was is no longer the question — 59
    resolved payment alerts stayed on the free route in the same replay.
    """
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high"}, 1, 1))
    _stub_common(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_SKIP_IMPORTANCE", "low")

    recovered = _info_payment_alert()
    recovered["parsed_data"]["status"] = "resolved"  # type: ignore[index]
    recovered["parsed_data"]["title"] = "[RESOLVED] 示例充值超限告警"  # type: ignore[index]

    result = await ai_analyzer.analyze_webhook_with_ai(recovered, alert_hash="h-pay-ok", skip_cache=True)

    assert result["_route_type"] == "rule_routed"
    llm_spy.assert_not_awaited()
