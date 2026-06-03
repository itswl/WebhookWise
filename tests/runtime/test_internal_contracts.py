from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from api import INTERNAL_ERROR_MESSAGE, internal_error_response
from services.webhooks import types as webhook_types
from tests.helpers.metric_helpers import MetricCall, StubMetric
from tests.helpers.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


def test_internal_protocol_keys_are_declared_in_one_place() -> None:
    keys = {
        webhook_types.ANALYSIS_ROUTE_TYPE,
        webhook_types.ANALYSIS_DEGRADED,
        webhook_types.ANALYSIS_DEGRADED_REASON,
        webhook_types.ANALYSIS_CACHE_HIT,
        webhook_types.ANALYSIS_CACHE_HIT_COUNT,
        webhook_types.ANALYSIS_PENDING,
        webhook_types.ANALYSIS_EMBEDDING,
        webhook_types.FORWARD_PENDING,
        webhook_types.OPENCLAW_RUN_ID,
        webhook_types.OPENCLAW_SESSION_KEY,
        webhook_types.FORWARD_DEGRADED,
        webhook_types.FORWARD_DEGRADED_REASON,
        webhook_types.OPENCLAW_TEXT,
        webhook_types.OPENCLAW_NEED_SUCCESS_NOTIFY,
        webhook_types.MANUAL_RETRY_STARTED_AT,
        webhook_types.WEBHOOK_ADAPTER,
    }
    allowed = {Path("services/webhooks/types.py")}
    offenders: list[str] = []
    for base in ("api", "services"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{rel}:{key}" for key in keys if re.search(rf"(?<![A-Za-z0-9_])[\"']{re.escape(key)}[\"']", text)
            )
    assert offenders == []


def test_internal_error_response_does_not_leak_exception_text() -> None:
    response = internal_error_response(detail="request-id")
    assert response.status_code == 500
    assert b"postgresql://" not in response.body
    assert b"Traceback" not in response.body
    assert INTERNAL_ERROR_MESSAGE.encode("utf-8") in response.body


def test_webhook_data_from_mapping_validates_runtime_boundary() -> None:
    data = webhook_types.webhook_data_from_mapping(
        {
            "source": "prometheus",
            "parsed_data": {"alert": "disk"},
            "body": "source-specific body text is allowed",
            "event": {"source_specific": True},
        }
    )

    assert data["source"] == "prometheus"
    assert data["parsed_data"] == {"alert": "disk"}
    assert data["body"] == "source-specific body text is allowed"

    try:
        webhook_types.webhook_data_from_mapping({"source": "custom", "source_native_field": "surprise"})
    except ValueError as exc:
        assert "source_native_field" in str(exc)
    else:
        raise AssertionError("strict WebhookData should reject undeclared fields")

    passthrough = webhook_types.webhook_data_from_mapping(
        {"source": "custom", "source_native_field": {"still": "json"}},
        strict=False,
    )
    assert dict(passthrough)["source_native_field"] == {"still": "json"}

    try:
        webhook_types.webhook_data_from_mapping({"parsed_data": "not-an-object"})
    except ValueError as exc:
        assert "parsed_data" in str(exc)
    else:
        raise AssertionError("invalid parsed_data should be rejected")

    try:
        webhook_types.webhook_data_from_mapping({1: "bad"})  # type: ignore[dict-item]
    except ValueError as exc:
        assert "non-string key" in str(exc)
    else:
        raise AssertionError("non-string keys should be rejected")


def test_noise_reduction_context_stores_related_ids_as_tuple() -> None:
    ctx = webhook_types.NoiseReductionContext("derived", 1, 0.9, True, "test", 2, [1, 2])

    assert ctx.related_alert_ids == (1, 2)
    assert isinstance(ctx.related_alert_ids, tuple)


async def test_dedup_read_failure_is_observable(monkeypatch: Any) -> None:
    from services import dedup

    async def fail_read(_: str) -> dict[str, Any]:
        raise RuntimeError("redis down")

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(dedup, "redis_get_json_dict", fail_read)
    monkeypatch.setattr(dedup, "REDIS_UNAVAILABLE_TOTAL", StubMetric(metric_calls, "redis_unavailable"))

    assert await dedup.get_dedup_state("alert-key") is None
    assert metric_calls == [("redis_unavailable", ("dedup", "read_allowed"), {}, "inc", 1)]


async def test_dedup_write_failure_is_observable(monkeypatch: Any) -> None:
    from services import dedup

    async def no_existing_state(_: str) -> None:
        return None

    async def fail_write(*_: Any) -> None:
        raise RuntimeError("redis down")

    metric_calls: list[MetricCall] = []
    monkeypatch.setattr(dedup, "get_dedup_state", no_existing_state)
    monkeypatch.setattr(dedup, "redis_setex_json", fail_write)
    monkeypatch.setattr(dedup, "REDIS_UNAVAILABLE_TOTAL", StubMetric(metric_calls, "redis_unavailable"))

    await dedup.remember_dedup_state("alert-key", 42, {"summary": "x"}, 60)
    assert metric_calls == [("redis_unavailable", ("dedup", "write_failed"), {}, "inc", 1)]
