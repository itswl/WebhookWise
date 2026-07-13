from unittest.mock import AsyncMock

import pytest


class _FakePermissionDenied(Exception):
    status_code = 403


_FakePermissionDenied.__name__ = "PermissionDeniedError"


class _FutureOpenAIProviderError(Exception):
    pass


_FutureOpenAIProviderError.__module__ = "openai._exceptions"


class _ApplicationBug(Exception):
    pass


@pytest.mark.asyncio
async def test_ai_policy_refusal_degrades_to_rules_even_when_global_degradation_disabled(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", False)
    warning_logs: list[tuple[object, ...]] = []

    async def fail_with_policy_refusal(
        parsed_data: dict[str, object], source: str, **kwargs: object
    ) -> tuple[dict[str, object], int, int]:
        raise _FakePermissionDenied("The request is prohibited due to a violation of provider Terms Of Service.")

    send_alert = AsyncMock()
    log_usage = AsyncMock()
    monkeypatch.setattr(ai_analyzer._llm_client, "_call_ai_with_retry", fail_with_policy_refusal)
    monkeypatch.setattr(ai_analyzer, "_send_ai_error_alert", send_alert)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", log_usage)
    monkeypatch.setattr(ai_analyzer.logger, "warning", lambda *args, **kwargs: warning_logs.append(args))

    result = await ai_analyzer.analyze_webhook_with_ai(
        {
            "source": "volcengine",
            "parsed_data": {"RuleName": "OpenRouterSuccessRateLow", "Level": "warning"},
        },
        alert_hash="hash-policy",
        skip_cache=True,
    )

    assert result["_route_type"] == "rule"
    assert result["_degraded"] is True
    assert str(result["_degraded_reason"]).startswith("llm_policy_refusal:")
    log_usage.assert_awaited_once_with("rule", "hash-policy", "volcengine")
    send_alert.assert_awaited_once()
    assert send_alert.await_args.kwargs["is_degraded"] is True
    assert warning_logs
    assert "error_type=%s" in str(warning_logs[0][0])
    assert warning_logs[0][2] == "PermissionDeniedError"


@pytest.mark.asyncio
async def test_ai_generic_error_still_raises_when_global_degradation_disabled(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", False)
    monkeypatch.setattr(ai_analyzer, "_send_ai_error_alert", AsyncMock())

    async def fail_generically(
        parsed_data: dict[str, object], source: str, **kwargs: object
    ) -> tuple[dict[str, object], int, int]:
        raise RuntimeError("unexpected provider failure")

    monkeypatch.setattr(ai_analyzer._llm_client, "_call_ai_with_retry", fail_generically)

    with pytest.raises(RuntimeError, match="unexpected provider failure"):
        await ai_analyzer.analyze_webhook_with_ai(
            {"source": "volcengine", "parsed_data": {"RuleName": "OtherAlert"}},
            alert_hash="hash-generic",
            skip_cache=True,
        )


@pytest.mark.asyncio
async def test_future_provider_exception_type_uses_degradation(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", True)

    async def fail_with_new_sdk_error(
        parsed_data: dict[str, object], source: str, **kwargs: object
    ) -> tuple[dict[str, object], int, int]:
        raise _FutureOpenAIProviderError("new sdk transport failure")

    send_alert = AsyncMock()
    log_usage = AsyncMock()
    monkeypatch.setattr(ai_analyzer._llm_client, "_call_ai_with_retry", fail_with_new_sdk_error)
    monkeypatch.setattr(ai_analyzer, "_send_ai_error_alert", send_alert)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", log_usage)

    result = await ai_analyzer.analyze_webhook_with_ai(
        {"source": "volcengine", "parsed_data": {"RuleName": "OtherAlert"}},
        alert_hash="hash-future-provider",
        skip_cache=True,
    )

    assert result["_route_type"] == "rule"
    assert result["_degraded"] is True
    assert str(result["_degraded_reason"]).startswith("ai_error:")
    send_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_application_exception_still_raises(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", False)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", True)

    async def fail_with_application_bug(
        parsed_data: dict[str, object], source: str, **kwargs: object
    ) -> tuple[dict[str, object], int, int]:
        raise _ApplicationBug("bad internal state")

    monkeypatch.setattr(ai_analyzer._llm_client, "_call_ai_with_retry", fail_with_application_bug)

    with pytest.raises(_ApplicationBug, match="bad internal state"):
        await ai_analyzer.analyze_webhook_with_ai(
            {"source": "volcengine", "parsed_data": {"RuleName": "OtherAlert"}},
            alert_hash="hash-application-bug",
            skip_cache=True,
        )
