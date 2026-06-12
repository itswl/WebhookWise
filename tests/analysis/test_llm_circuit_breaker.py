"""Tests for the LLM analysis circuit breaker."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_open_breaker_degrades_to_rules(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from core.circuit_breaker import CircuitBreakerOpenException
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    # degradation_enabled OFF — the open-breaker path must degrade regardless.
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", False)

    async def open_breaker(*_a: object, **_k: object) -> tuple[dict[str, object], int, int]:
        raise CircuitBreakerOpenException("llm")

    log_usage = AsyncMock()
    send_alert = AsyncMock()
    monkeypatch.setattr(ai_analyzer._llm_client, "call_ai_with_breaker", open_breaker)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", log_usage)
    monkeypatch.setattr(ai_analyzer, "_send_ai_error_alert", send_alert)

    result = await ai_analyzer.analyze_webhook_with_ai(
        {"source": "prometheus", "parsed_data": {"RuleName": "CPUHigh", "Level": "critical"}},
        alert_hash="hash-cb",
        skip_cache=True,
    )

    assert result["_route_type"] == "rule"
    assert result["_degraded"] is True
    assert result["_degraded_reason"] == "llm_circuit_open"
    log_usage.assert_awaited_once_with("rule", "hash-cb", "prometheus")
    # Open-breaker degradation is silent (notify=False) — no error alert spam.
    send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_ai_with_breaker_wraps_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.analysis import ai_llm_client, circuit_breakers

    captured: dict[str, object] = {}

    async def fake_call(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        captured["func"] = func
        captured["args"] = args
        return ({"summary": "ok"}, 1, 2)

    # Verify the breaker is the one invoking _call_ai_with_retry.
    monkeypatch.setattr(circuit_breakers.llm_cb, "call_async", fake_call)
    result = await ai_llm_client.call_ai_with_breaker({"k": "v"}, "src")

    assert result == ({"summary": "ok"}, 1, 2)
    assert captured["func"] is ai_llm_client._call_ai_with_retry
    assert captured["args"] == ({"k": "v"}, "src")
