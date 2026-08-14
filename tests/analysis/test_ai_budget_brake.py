"""The monthly budget as a brake: at 100% the analysis degrades instead of paying."""

from unittest.mock import AsyncMock

import pytest


def _alert() -> dict[str, object]:
    return {"source": "prometheus", "parsed_data": {"RuleName": "ServiceDownCritical", "Level": "critical"}}


def _stub(monkeypatch, temp_config, llm_spy) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "AI_ROUTING_ENABLED", False)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", AsyncMock())
    monkeypatch.setattr(ai_analyzer, "_send_ai_error_alert", AsyncMock())
    monkeypatch.setattr(ai_analyzer._llm_client, "call_ai_with_breaker", llm_spy)


@pytest.mark.asyncio
async def test_spending_stops_at_the_budget_and_says_why(monkeypatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    llm_spy = AsyncMock(return_value=({"importance": "high", "summary": "x"}, 1, 1))
    _stub(monkeypatch, temp_config, llm_spy)
    monkeypatch.setattr(ai_analyzer, "budget_exhausted", AsyncMock(return_value=(True, 21.0, 20.0)))

    result = await ai_analyzer.analyze_webhook_with_ai(_alert(), alert_hash="h-broke", skip_cache=True)

    llm_spy.assert_not_awaited()
    assert result["_route_type"] == "rule"
    # A refusal to spend is a degradation, unlike tiered routing, and must be
    # legible as one: an operator has to be able to tell the two apart.
    assert result["_degraded"] is True
    assert "budget_exhausted" in result["_degraded_reason"]


@pytest.mark.asyncio
async def test_an_unreadable_meter_never_blocks_analysis(monkeypatch, temp_config) -> None:
    """A broken budget reader must fail open — losing analysis over accounting
    is a worse outcome than overspending by one interval."""
    from services.analysis import ai_budget

    class _Notif:
        AI_COST_MONTHLY_BUDGET_USD = 20.0
        AI_COST_BUDGET_ENFORCE = True

    monkeypatch.setattr(ai_budget, "get_config_manager", lambda: type("C", (), {"notifications": _Notif})())
    monkeypatch.setattr(ai_budget, "month_to_date_spend", AsyncMock(side_effect=RuntimeError("db down")))

    exhausted, spent, budget = await ai_budget.budget_exhausted()

    assert exhausted is False
    assert budget == 20.0


@pytest.mark.asyncio
async def test_the_brake_is_off_until_a_deployment_asks_for_it(monkeypatch, temp_config) -> None:
    from services.analysis import ai_budget

    class _Notif:
        AI_COST_MONTHLY_BUDGET_USD = 20.0
        AI_COST_BUDGET_ENFORCE = False

    spend = AsyncMock(return_value=999.0)
    monkeypatch.setattr(ai_budget, "get_config_manager", lambda: type("C", (), {"notifications": _Notif})())
    monkeypatch.setattr(ai_budget, "month_to_date_spend", spend)

    exhausted, _, _ = await ai_budget.budget_exhausted()

    assert exhausted is False
    spend.assert_not_awaited()  # and it does not touch the database to find out
