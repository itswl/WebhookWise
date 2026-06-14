from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.helpers.metric_helpers import MetricCall, StubMetric


def test_retry_policy_classifies_wrapped_status_codes_and_openai_like_errors() -> None:
    from core.retry_policies import RetryPolicy

    policy = RetryPolicy()

    request = httpx.Request("GET", "https://example.test")
    retryable_status = httpx.HTTPStatusError(
        "bad gateway",
        request=request,
        response=httpx.Response(502, request=request),
    )
    terminal_status = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(422, request=request),
    )
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = retryable_status

    class RateLimitish(Exception):
        status_code = 429

    RateLimitish.__name__ = "RateLimitError"

    class Authish(Exception):
        body = {"error": {"code": "content_policy_violation", "message": "blocked"}}

    Authish.__name__ = "AuthenticationError"

    assert policy.should_retry(wrapped)
    assert not policy.should_retry(terminal_status)
    assert policy.should_retry(RateLimitish("slow down"))
    assert not policy.should_retry(Authish("nope"))
    assert not policy.should_retry(ValueError("bad payload"))


def test_retry_policy_handles_response_status_attrs_error_codes_and_cycles() -> None:
    from core.retry_policies import RetryPolicy

    policy = RetryPolicy()

    retryable = RuntimeError("outer")
    retryable.response = SimpleNamespace(status_code=503)
    terminal = RuntimeError("outer")
    terminal.status_code = 404
    context_policy = RuntimeError("outer")
    policy_error = RuntimeError("inner")
    policy_error.body = {"error": {"code": "context_length_exceeded"}}
    context_policy.__context__ = policy_error
    cycle = RuntimeError("cycle")
    cycle.__cause__ = cycle

    assert policy.should_retry(retryable)
    assert not policy.should_retry(terminal)
    assert not policy.should_retry(context_policy)
    assert not policy.should_retry(cycle)


@pytest.mark.asyncio
async def test_dedup_state_read_write_errors_and_payload_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import dedup

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(dedup, "REDIS_UNAVAILABLE_TOTAL", StubMetric(metric_calls, "REDIS_UNAVAILABLE_TOTAL"))

    async def read_error(_key: str) -> dict[str, object]:
        raise RuntimeError("redis down")

    monkeypatch.setattr(dedup, "redis_get_json_dict", read_error)
    assert await dedup.get_dedup_state("alert") is None
    assert metric_calls[-1][:4] == ("REDIS_UNAVAILABLE_TOTAL", ("dedup", "read_allowed"), {}, "inc")

    async def read_bad(_key: str) -> dict[str, object]:
        return {"original_event_id": "not-int"}

    monkeypatch.setattr(dedup, "redis_get_json_dict", read_bad)
    assert await dedup.get_dedup_state("alert") is None

    async def read_good(_key: str) -> dict[str, object]:
        return {
            "original_event_id": "42",
            "first_seen_at": "10.5",
            "last_seen_at": "12.5",
            "count": "3",
            "analysis": {"importance": "high"},
        }

    monkeypatch.setattr(dedup, "redis_get_json_dict", read_good)
    state = await dedup.get_dedup_state("alert")
    assert state is not None
    assert state.original_event_id == 42
    assert state.count == 3
    assert state.analysis == {"importance": "high"}

    # remember_dedup_state now does an atomic read-modify-write via a single Lua
    # script (redis_eval_int); count/first_seen_at are computed server-side.
    evals: list[tuple[int, tuple[object, ...]]] = []

    async def eval_ok(_script: str, numkeys: int, *args: object) -> int:
        evals.append((numkeys, args))
        return 4  # script returns the new count

    monkeypatch.setattr(dedup, "redis_eval_int", eval_ok)
    await dedup.remember_dedup_state("alert", 42, {"importance": "low"}, 10)
    # Args: (key, original_event_id, now, ttl, reset_chain, dedup_key, analysis_json).
    # numkeys=1; TTL (args index 3) floored to 60.
    assert evals[-1][0] == 1
    assert int(evals[-1][1][3]) == 60

    async def eval_error(_script: str, _numkeys: int, *_args: object) -> int:
        raise RuntimeError("write down")

    monkeypatch.setattr(dedup, "redis_eval_int", eval_error)
    await dedup.remember_dedup_state("alert", 42, None, 120, reset_chain=True)
    assert metric_calls[-1][:4] == ("REDIS_UNAVAILABLE_TOTAL", ("dedup", "write_failed"), {}, "inc")


@pytest.mark.asyncio
async def test_resolve_dedup_reuse_rechain_db_fallback_and_new(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from services import dedup

    monkeypatch.setattr(temp_config.retry, "DEDUP_WINDOW_SECONDS", 60)
    monkeypatch.setattr(temp_config.retry, "ANALYSIS_REUSE_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(dedup.time, "time", lambda: 1000.0)

    async def reuse_state(_key: str) -> dedup.DedupState:
        return dedup.DedupState(7, 970.0, 995.0, 2, {"importance": "high"})

    monkeypatch.setattr(dedup, "get_dedup_state", reuse_state)
    reuse = await dedup.resolve_dedup("key")
    assert reuse.action == dedup.DedupAction.REUSE
    assert reuse.route_type == "redis_reuse"

    async def rechain_state(_key: str) -> dedup.DedupState:
        return dedup.DedupState(7, 800.0, 995.0, 9, {"importance": "high"})

    monkeypatch.setattr(dedup, "get_dedup_state", rechain_state)
    rechain = await dedup.resolve_dedup("key")
    assert rechain.action == dedup.DedupAction.RECHAIN
    assert rechain.route_type == "rechain"
    assert rechain.reset_chain is True

    async def db_miss(_key: str, _window: int) -> None:
        return None

    async def stale_reusable_state(_key: str) -> dedup.DedupState:
        return dedup.DedupState(7, 100.0, 500.0, 9, {"importance": "high"})

    monkeypatch.setattr(dedup, "get_dedup_state", stale_reusable_state)
    monkeypatch.setattr(dedup, "_find_original_by_dedup_key", db_miss)
    new_from_stale = await dedup.resolve_dedup("key")
    assert new_from_stale.action == dedup.DedupAction.NEW
    assert new_from_stale.reset_chain is True

    async def stale_or_pending_state(_key: str) -> dedup.DedupState:
        return dedup.DedupState(7, 100.0, 500.0, 9, {"_pending": True})

    async def db_hit(_key: str, window: int) -> dict[str, object]:
        assert window == 60
        return {"analysis": {"importance": "medium"}, "original_event_id": 99}

    monkeypatch.setattr(dedup, "get_dedup_state", stale_or_pending_state)
    monkeypatch.setattr(dedup, "_find_original_by_dedup_key", db_hit)
    fallback = await dedup.resolve_dedup("key")
    assert fallback.action == dedup.DedupAction.REUSE
    assert fallback.route_type == "db_reuse"
    assert fallback.original_event_id == 99

    monkeypatch.setattr(dedup, "_find_original_by_dedup_key", db_miss)
    new = await dedup.resolve_dedup("key")
    assert new.action == dedup.DedupAction.NEW
    assert new.reset_chain is True

    async def no_state(_key: str) -> None:
        return None

    monkeypatch.setattr(dedup, "get_dedup_state", no_state)
    brand_new = await dedup.resolve_dedup("key")
    assert brand_new.action == dedup.DedupAction.NEW
    assert brand_new.reset_chain is False


def test_generate_event_keys_fallback_uses_payload_hash() -> None:
    from services import dedup

    alert_hash, dedup_key = dedup.generate_event_keys({"unstructured": "value"}, " CustomSource ")

    assert alert_hash == dedup_key
    assert len(alert_hash) == 64


def test_payload_sanitizer_strips_depth_limits_and_truncates_values() -> None:
    from core.sensitive_data import REDACTED
    from services.webhooks.payload_sanitizer import _should_offload, _truncate_large_values, sanitize_for_ai
    from services.webhooks.policies import PayloadPolicy

    policy = PayloadPolicy(offload_threshold_bytes=8, strip_keys=frozenset({"raw_trace"}), max_bytes=120)
    nested = {
        "keep": "ok",
        "raw_trace": "remove me",
        "token": "secret-token",
        "child": {"raw_trace": "remove me too", "larger": "y" * 600, "big": "x" * 500},
    }

    cleaned = sanitize_for_ai(nested, policy=policy)

    assert "raw_trace" not in cleaned
    assert cleaned["token"] == REDACTED
    assert cleaned["child"]["big"].startswith("x")
    direct_truncated = _truncate_large_values({"larger": "y" * 600, "big": "x" * 500}, max_bytes=120)
    assert "truncated" in direct_truncated["big"]
    assert _should_offload({"large": "x" * 8}, policy)
    assert _should_offload([{"large": "x" * 8}], policy)
    assert not _should_offload({"deep": {"a": {"b": {"c": "x" * 100}}}}, policy, depth=3)


def test_payload_sanitizer_list_and_recursion_guard() -> None:
    from services.webhooks.payload_sanitizer import _strip_keys_recursive, _truncate_large_values

    deeply_nested: object = {"a": [{"b": {"c": "value"}}]}
    stripped = _strip_keys_recursive(deeply_nested, {"c"}, max_depth=2)
    assert stripped == {"a": [{"_truncated": True, "_reason": "max recursion depth 2"}]}

    truncated_list = _truncate_large_values(list(range(20)), max_bytes=10)
    assert isinstance(truncated_list, list)
    # Head + tail are kept (recent items matter); the elision marker sits between.
    assert truncated_list[0] == 0
    assert truncated_list[-1] == 19
    marker = next(x for x in truncated_list if isinstance(x, dict) and x.get("_truncated"))
    assert marker["_original_length"] == 20
    assert _truncate_large_values({"too": {"deep": "x"}}, max_bytes=1, depth=6) == {
        "_truncated": True,
        "_reason": "max depth exceeded",
    }


def test_ai_error_helpers_extract_nested_messages_and_policy_refusals() -> None:
    from services.analysis import ai_analyzer

    inner = RuntimeError("fallback text")
    outer = RuntimeError("")
    outer.__cause__ = inner
    assert ai_analyzer._extract_ai_error_message(outer) == "fallback text"

    body_error = RuntimeError("outer")
    body_error.body = {"error": {"message": " explicit provider message ", "code": "content_policy_violation"}}
    assert ai_analyzer._extract_ai_error_message(body_error) == "explicit provider message"
    assert ai_analyzer._is_ai_policy_refusal(body_error)

    empty_error = RuntimeError("")
    assert ai_analyzer._extract_ai_error_message(empty_error) == "RuntimeError"


@pytest.mark.asyncio
async def test_analyze_webhook_ai_cache_hit_disabled_and_success_paths(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_DEGRADATION", True)

    usage_calls: list[tuple[object, ...]] = []

    async def log_usage(*args: object, **kwargs: object) -> None:
        usage_calls.append((*args, kwargs))

    async def cached(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"importance": "high", "summary": "cached", "_cache_hit_count": 3}

    monkeypatch.setattr(ai_analyzer, "get_cached_analysis", cached)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", log_usage)

    result = await ai_analyzer.analyze_webhook_with_ai(
        {"source": "prometheus", "parsed_data": {"RuleName": "CacheHit"}},
        alert_hash="hash-cache",
    )
    assert result["_route_type"] == "cache"
    assert usage_calls[-1][0] == "cache"

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", False)
    disabled = await ai_analyzer.analyze_webhook_with_ai(
        {"source": "prometheus", "parsed_data": {"RuleName": "Disabled"}},
        alert_hash="hash-disabled",
        skip_cache=True,
    )
    assert disabled["_route_type"] == "rule"
    assert disabled["_degraded"] is True
    assert disabled["_degraded_reason"] == "disabled"

    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    saved: list[dict[str, object]] = []

    async def ai_success(*_args: object, **_kwargs: object) -> tuple[dict[str, object], int, int]:
        return {"importance": "medium", "summary": "ok"}, 12, 3

    async def save_cache(_key: str, analysis: dict[str, object], **_kwargs: object) -> None:
        saved.append(analysis)

    async def cache_miss(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(ai_analyzer, "get_cached_analysis", cache_miss)
    monkeypatch.setattr(ai_analyzer._llm_client, "_call_ai_with_retry", ai_success)
    monkeypatch.setattr(ai_analyzer, "save_to_cache", save_cache)
    success = await ai_analyzer.analyze_webhook_with_ai(
        {"source": "prometheus", "parsed_data": {"RuleName": "Success"}},
        alert_hash="hash-success",
    )
    assert success["_route_type"] == "ai"
    assert saved == [{"importance": "medium", "summary": "ok"}]


@pytest.mark.asyncio
async def test_analyze_webhook_ai_cache_hit_promotes_gpu_high(
    monkeypatch: pytest.MonkeyPatch,
    temp_config: Any,
) -> None:
    from services.analysis import ai_analyzer

    monkeypatch.setattr(temp_config.ai, "CACHE_ENABLED", True)
    monkeypatch.setattr(temp_config.ai, "ENABLE_AI_ANALYSIS", True)
    monkeypatch.setattr(temp_config.ai, "OPENAI_API_KEY", "sk-test")

    async def cached(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"importance": "medium", "summary": "GPU使用率100%达上限"}

    async def log_usage(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(ai_analyzer, "get_cached_analysis", cached)
    monkeypatch.setattr(ai_analyzer, "log_ai_usage", log_usage)

    result = await ai_analyzer.analyze_webhook_with_ai(
        {
            "source": "volcengine",
            "parsed_data": {
                "RuleName": "云服务器GPU卡告警",
                "SubNamespace": "GPU",
                "Resources": [
                    {
                        "Metrics": [
                            {"Name": "GpuUsedUtilization", "CurrentValue": 100, "Threshold": 80},
                            {"Name": "GpuMemoryUsedUtilization", "CurrentValue": 87.2, "Threshold": 90},
                        ]
                    }
                ],
            },
        },
        alert_hash="hash-gpu-cache",
    )

    assert result["importance"] == "high"
    assert result["_route_type"] == "cache"
    assert result["_importance_override"] == "gpu_high"
