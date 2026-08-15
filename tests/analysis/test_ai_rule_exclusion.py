"""Alert rules an operator has decided are never worth a model call."""

from unittest.mock import AsyncMock

import pytest

EXCLUDED = "示例充值超限告警,示例提现超限告警"


def _topup_alert() -> dict[str, object]:
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


def _stub(monkeypatch, temp_config, llm_spy) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", False)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", AsyncMock())
    monkeypatch.setattr(ai_analyzer._llm_client, "call_ai_with_breaker", llm_spy)


@pytest.mark.asyncio
async def test_an_excluded_rule_never_reaches_the_model_but_keeps_its_severity(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "x"}, 1, 1))
    _stub(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_EXCLUDED_RULES", EXCLUDED)

    result = await ai_analyzer.analyze_webhook_with_ai(_topup_alert(), alert_hash="h-topup", skip_cache=True)

    llm_spy.assert_not_awaited()
    assert result["_route_type"] == "rule_excluded"
    # Not a degradation: nothing failed, this is policy.
    assert result.get("_degraded") is not True
    # And it is still a payment alert, so it is still forwarded as one.
    assert result["importance"] == "high"


@pytest.mark.asyncio
async def test_an_unlisted_rule_is_untouched(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "x"}, 1, 1))
    _stub(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_EXCLUDED_RULES", EXCLUDED)

    other = _topup_alert()
    other["parsed_data"]["RuleName"] = "支付网关5xx突增"  # type: ignore[index]

    result = await ai_analyzer.analyze_webhook_with_ai(other, alert_hash="h-5xx", skip_cache=True)

    llm_spy.assert_awaited()
    assert result["_route_type"] == "ai"


@pytest.mark.asyncio
async def test_matching_is_exact_not_substring(monkeypatch, temp_config) -> None:
    """A neighbouring rule with a longer name must not be silenced by accident."""
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "x"}, 1, 1))
    _stub(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(temp_config.ai, "AI_EXCLUDED_RULES", EXCLUDED)

    near = _topup_alert()
    near["parsed_data"]["RuleName"] = "示例充值超限告警-示例账户"  # type: ignore[index]

    await ai_analyzer.analyze_webhook_with_ai(near, alert_hash="h-near", skip_cache=True)

    llm_spy.assert_awaited()


def test_the_rule_name_is_read_from_grafana_labels_too() -> None:
    from services.webhooks.inbound_rules import alert_rule_name

    assert alert_rule_name({"RuleName": "A"}) == "A"
    assert alert_rule_name({"commonLabels": {"alertname": "B"}}) == "B"
    assert alert_rule_name({}) == ""
