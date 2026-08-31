"""The budget brake's mode ladder: off computes nothing, shadow records and spends."""

from unittest.mock import AsyncMock

import pytest


def _notif(**overrides: object) -> object:
    class _Notif:
        AI_COST_MONTHLY_BUDGET_USD = 20.0
        AI_COST_BUDGET_ENFORCE = False
        AI_COST_BUDGET_MODE = ""

    for key, value in overrides.items():
        setattr(_Notif, key, value)
    return type("C", (), {"notifications": _Notif})()


@pytest.mark.asyncio
async def test_shadow_records_the_refusal_and_spends_anyway(monkeypatch, temp_config) -> None:
    from services.analysis import ai_budget

    monkeypatch.setattr(ai_budget, "get_config_manager", lambda: _notif(AI_COST_BUDGET_MODE="shadow"))
    monkeypatch.setattr(ai_budget, "month_to_date_spend", AsyncMock(return_value=25.0))
    signals: list[tuple[str, str]] = []
    monkeypatch.setattr(ai_budget, "record_signal", lambda name, state, attrs=None: signals.append((name, state)))

    exhausted, spent, budget = await ai_budget.budget_exhausted()

    assert exhausted is False  # the call still goes through
    assert spent == 25.0 and budget == 20.0
    assert ("ai.budget", "shadow_exhausted") in signals  # ...but the ledger knows


@pytest.mark.asyncio
async def test_shadow_below_budget_records_nothing(monkeypatch, temp_config) -> None:
    from services.analysis import ai_budget

    monkeypatch.setattr(ai_budget, "get_config_manager", lambda: _notif(AI_COST_BUDGET_MODE="shadow"))
    monkeypatch.setattr(ai_budget, "month_to_date_spend", AsyncMock(return_value=3.0))
    signals: list[tuple[str, str]] = []
    monkeypatch.setattr(ai_budget, "record_signal", lambda name, state, attrs=None: signals.append((name, state)))

    exhausted, _, _ = await ai_budget.budget_exhausted()

    assert exhausted is False
    assert signals == []


@pytest.mark.asyncio
async def test_mode_setting_beats_the_legacy_boolean(monkeypatch, temp_config) -> None:
    """`mode=off` must win even when the old boolean still says enforce."""
    from services.analysis import ai_budget

    spend = AsyncMock(return_value=999.0)
    monkeypatch.setattr(
        ai_budget,
        "get_config_manager",
        lambda: _notif(AI_COST_BUDGET_ENFORCE=True, AI_COST_BUDGET_MODE="off"),
    )
    monkeypatch.setattr(ai_budget, "month_to_date_spend", spend)

    exhausted, _, _ = await ai_budget.budget_exhausted()

    assert exhausted is False
    spend.assert_not_awaited()  # off computes nothing, exactly like the old False


@pytest.mark.asyncio
async def test_legacy_enforce_true_still_enforces_with_mode_unset(monkeypatch, temp_config) -> None:
    from services.analysis import ai_budget

    monkeypatch.setattr(ai_budget, "get_config_manager", lambda: _notif(AI_COST_BUDGET_ENFORCE=True))
    monkeypatch.setattr(ai_budget, "month_to_date_spend", AsyncMock(return_value=21.0))

    exhausted, spent, budget = await ai_budget.budget_exhausted()

    assert exhausted is True and spent == 21.0 and budget == 20.0
