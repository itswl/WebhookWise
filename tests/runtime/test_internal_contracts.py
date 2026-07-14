from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from api import INTERNAL_ERROR_MESSAGE, internal_error_response
from contracts import webhook_payload
from services.webhooks import types as webhook_types
from tests.helpers.metric_helpers import MetricCall, StubMetric
from tests.helpers.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


def test_core_and_adapter_dependency_direction_is_enforced() -> None:
    forbidden_core_imports: list[str] = []
    for path in (ROOT / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        forbidden_core_imports.extend(
            f"{path.relative_to(ROOT)}:{pattern}"
            for pattern in (
                "from api.",
                "import api.",
                "from services.",
                "import services.",
                "from adapters.",
                "import adapters.",
            )
            if pattern in text
        )

    adapter_type_imports: list[str] = []
    for path in (ROOT / "adapters").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "services.webhooks.types" in text:
            adapter_type_imports.append(str(path.relative_to(ROOT)))

    assert forbidden_core_imports == []
    assert adapter_type_imports == []


def test_openclaw_compatibility_facade_is_not_reintroduced() -> None:
    assert not (ROOT / "services/analysis/openclaw.py").exists()

    offenders: list[str] = []
    for base in ("api", "services", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if (
                "from services.analysis.openclaw import" in text
                or "import services.analysis.openclaw" in text
                or "services.analysis.openclaw." in text
            ):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


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
        webhook_payload.WEBHOOK_ADAPTER,
    }
    allowed = {Path("services/webhooks/types.py"), Path("contracts/webhook_payload.py")}
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
    data = webhook_payload.webhook_data_from_mapping(
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
        webhook_payload.webhook_data_from_mapping({"source": "custom", "source_native_field": "surprise"})
    except ValueError as exc:
        assert "source_native_field" in str(exc)
    else:
        raise AssertionError("strict WebhookData should reject undeclared fields")

    passthrough = webhook_payload.webhook_data_from_mapping(
        {"source": "custom", "source_native_field": {"still": "json"}},
        strict=False,
    )
    assert dict(passthrough)["source_native_field"] == {"still": "json"}

    try:
        webhook_payload.webhook_data_from_mapping({"parsed_data": "not-an-object"})
    except ValueError as exc:
        assert "parsed_data" in str(exc)
    else:
        raise AssertionError("invalid parsed_data should be rejected")

    try:
        webhook_payload.webhook_data_from_mapping({1: "bad"})  # type: ignore[dict-item]
    except ValueError as exc:
        assert "non-string key" in str(exc)
    else:
        raise AssertionError("non-string keys should be rejected")


def test_webhook_data_validate_only_mode_matches_copy_mode() -> None:
    """copy=False must validate identically to copy=True, minus the rebuild."""
    payload = {
        "source": "prometheus",
        "parsed_data": {"alert": "disk", "nested": {"deep": [1, 2, {"k": "v"}]}},
        "Resources": [{"InstanceId": "i-1", "Dimensions": [{"Name": "Host", "Value": "h1"}]}],
        "body": ["raw", {"x": 1}],
        "source_native_field": {"still": "json"},
    }

    copied = webhook_payload.webhook_data_from_mapping(payload, strict=False)
    shared = webhook_payload.webhook_data_from_mapping(payload, strict=False, copy=False)

    # Identical content either way.
    assert dict(copied) == dict(shared)
    # copy=True rebuilds containers; copy=False shares the caller's tree.
    assert copied["parsed_data"] is not payload["parsed_data"]
    assert shared["parsed_data"] is payload["parsed_data"]
    assert shared["Resources"] is payload["Resources"]

    # Validation behaviour and error messages are identical in both modes.
    for bad in (
        {"parsed_data": "not-an-object"},
        {"source": "x", "weird": {"k": object()}},
        {"source": "x", "raw": {1: "bad-key"}},
    ):
        errors: list[str] = []
        for copy_mode in (True, False):
            try:
                webhook_payload.webhook_data_from_mapping(bad, strict=False, copy=copy_mode)  # type: ignore[arg-type]
            except ValueError as exc:
                errors.append(str(exc))
            else:
                errors.append("<no error>")
        assert errors[0] == errors[1]
        assert errors[0] != "<no error>"


def test_adapter_normalization_hash_is_stable_across_repeated_runs() -> None:
    """The ingress backpressure hash and the worker hash must agree: both run
    the adapter normalizer over the same body, so repeated normalization of one
    payload must produce identical alert/dedup keys."""
    import json as stdlib_json

    from adapters.ecosystem_adapters import normalize_webhook_event
    from services.dedup import generate_event_keys

    raw_body = stdlib_json.dumps(
        {
            "Namespace": "VCM_ECS",
            "RuleName": "HighCPU",
            "Level": "critical",
            "Resources": [{"InstanceId": "i-abc123", "Dimensions": [{"Name": "Host", "Value": "prod-1"}]}],
        }
    ).encode()

    keys: list[tuple[str, str]] = []
    for _ in range(2):  # ingress pass + worker pass
        payload = stdlib_json.loads(raw_body)
        normalized = normalize_webhook_event(payload, "volcengine")
        keys.append(generate_event_keys(dict(normalized.data), normalized.source))

    assert keys[0] == keys[1]
    assert keys[0][0]  # non-empty alert hash


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

    async def fail_write(*_: Any) -> None:
        raise RuntimeError("redis down")

    metric_calls: list[MetricCall] = []
    # remember_dedup_state now writes via an atomic Lua script (redis_eval_int).
    monkeypatch.setattr(dedup, "redis_eval_int", fail_write)
    monkeypatch.setattr(dedup, "REDIS_UNAVAILABLE_TOTAL", StubMetric(metric_calls, "redis_unavailable"))

    await dedup.remember_dedup_state("alert-key", 42, {"summary": "x"}, 60)
    assert metric_calls == [("redis_unavailable", ("dedup", "write_failed"), {}, "inc", 1)]
