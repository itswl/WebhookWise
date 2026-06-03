from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any


def test_emit_event_records_metric_span_event_and_structured_log(monkeypatch) -> None:
    from core.observability import events
    from core.observability.attributes import WEBHOOK_SOURCE

    metric_names: list[str] = []
    span_events: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        events.OBSERVABILITY_EVENTS_TOTAL,
        "labels",
        lambda name: SimpleNamespace(inc=lambda: metric_names.append(name)),
    )
    monkeypatch.setattr(events, "add_span_event", lambda name, attrs: span_events.append((name, dict(attrs))))

    logger = logging.getLogger("webhook_service.events")
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = CaptureHandler()
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    old_level = logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        events.emit_event("webhook.test", {WEBHOOK_SOURCE: "github"}, body="test event")
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)

    assert metric_names == ["webhook.test"]
    assert span_events == [
        (
            "webhook.test",
            {
                "webhook.source": "github",
                "event.name": "webhook.test",
            },
        )
    ]
    assert records[0].getMessage() == "test event"
    assert getattr(records[0], "event.name") == "webhook.test"
    assert getattr(records[0], "webhook.source") == "github"


def test_setup_profiling_configures_pyroscope(monkeypatch) -> None:
    from core.observability import profiling

    configured: dict[str, Any] = {}
    emitted: list[tuple[str, dict[str, object]]] = []

    fake_pyroscope = SimpleNamespace(configure=lambda **kwargs: configured.update(kwargs))
    monkeypatch.setitem(sys.modules, "pyroscope", fake_pyroscope)
    monkeypatch.setattr(profiling, "_initialized", False)
    monkeypatch.setattr(profiling, "_register_span_profiles", lambda: None)
    monkeypatch.setattr(profiling, "emit_event", lambda name, attrs: emitted.append((name, dict(attrs))))
    monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("PYROSCOPE_SERVER_ADDRESS", "http://pyroscope:4040")
    monkeypatch.setenv("PYROSCOPE_SAMPLE_RATE", "50")
    monkeypatch.setenv("PYROSCOPE_TAGS", "region=local,team.name=platform")

    profiling.setup_profiling(service_name="webhookwise-api")

    assert configured["application_name"] == "webhookwise-api"
    assert configured["server_address"] == "http://pyroscope:4040"
    assert configured["sample_rate"] == 50
    assert "service_name" not in configured["tags"]
    assert configured["tags"]["region"] == "local"
    assert configured["tags"]["team_name"] == "platform"
    assert emitted == [("profiles.started", {"profile.backend": "pyroscope", "profile.application": "webhookwise-api"})]


def test_component_metric_label_contracts_are_low_cardinality() -> None:
    from core.observability import metrics

    expected_label_keys = {
        "SECURITY_CHECKS_TOTAL": ("security.check", "security.result"),
        "WEBHOOK_INGRESS_PAYLOAD_BYTES": ("webhook.source", "webhook.outcome"),
        "WEBHOOK_INGRESS_REQUESTS_TOTAL": ("webhook.source", "webhook.outcome"),
        "WEBHOOK_INGRESS_REQUEST_DURATION_SECONDS": ("webhook.source", "webhook.outcome"),
        "WEBHOOK_PIPELINE_STEP_TOTAL": ("pipeline.step", "webhook.source", "webhook.outcome"),
        "WEBHOOK_PIPELINE_STEP_DURATION_SECONDS": ("pipeline.step", "webhook.source", "webhook.outcome"),
        "WEBHOOK_DEDUP_DECISIONS_TOTAL": ("webhook.source", "dedup.action"),
        "WEBHOOK_DEDUP_DURATION_SECONDS": ("webhook.source", "dedup.action"),
        "WEBHOOK_ANALYSIS_RESULTS_TOTAL": (
            "webhook.source",
            "webhook.route",
            "webhook.importance",
            "ai.degraded",
        ),
        "WEBHOOK_FORWARD_DECISIONS_TOTAL": (
            "webhook.source",
            "forward.decision",
            "forward.reason",
            "forward.target_type",
        ),
        "WEBHOOK_NOISE_EVALUATIONS_TOTAL": ("webhook.source", "webhook.relation", "webhook.suppressed"),
        "WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS": ("webhook.source", "webhook.relation", "webhook.suppressed"),
        "AI_REQUESTS_TOTAL": ("webhook.source", "ai.engine", "ai.status"),
        "AI_CACHE_REQUESTS_TOTAL": ("ai.cache.operation", "ai.cache.result"),
        "AI_CACHE_OPERATION_DURATION_SECONDS": ("ai.cache.operation", "ai.cache.result"),
        "AI_DEGRADATIONS_TOTAL": ("ai.degradation.reason",),
        "WORKER_TASKS_TOTAL": ("worker.task.name", "worker.task.status"),
        "WORKER_TASK_DURATION_SECONDS": ("worker.task.name", "worker.task.status"),
        "FORWARD_DELIVERY_TOTAL": ("forward.target_type", "forward.status"),
        "FORWARD_DELIVERY_DURATION_SECONDS": ("forward.target_type", "forward.status"),
        "FORWARD_OUTBOX_RECORDS_TOTAL": ("forward.target_type", "forward.status"),
        "FORWARD_OUTBOX_PROCESS_DURATION_SECONDS": ("forward.target_type", "forward.status"),
        "FORWARD_OUTBOX_BACKLOG_AGE_SECONDS": ("forward.target_type", "forward.status"),
        "QUEUE_OPERATIONS_TOTAL": ("queue.name", "queue.operation", "queue.status"),
        "QUEUE_OPERATION_DURATION_SECONDS": ("queue.name", "queue.operation", "queue.status"),
        "DB_HEALTH_STATE": ("db.state",),
        "DB_SESSION_TOTAL": ("db.operation", "db.status"),
        "DB_SESSION_DURATION_SECONDS": ("db.operation", "db.status"),
        "REDIS_OPERATIONS_TOTAL": ("redis.operation", "redis.status"),
        "REDIS_OPERATION_DURATION_SECONDS": ("redis.operation", "redis.status"),
        "REDIS_HEALTH_STATE": ("redis.state",),
        "CIRCUIT_BREAKER_REQUESTS_TOTAL": ("circuit_breaker.name", "circuit_breaker.outcome"),
        "CIRCUIT_BREAKER_TRANSITIONS_TOTAL": ("circuit_breaker.name", "circuit_breaker.state"),
        "CIRCUIT_BREAKER_STATE": ("circuit_breaker.name", "circuit_breaker.state"),
    }

    forbidden_labels = {"webhook.event_id", "webhook.alert_hash", "forward.target", "url", "request_id"}
    for metric_name, label_keys in expected_label_keys.items():
        metric = getattr(metrics, metric_name)
        assert metric.label_keys == label_keys
        assert forbidden_labels.isdisjoint(metric.label_keys)


def test_sanitize_source_preserves_custom_low_cardinality_values() -> None:
    from core.observability.metrics import sanitize_source

    assert sanitize_source(" OpenClaw ") == "openclaw"
    assert sanitize_source("Custom Team / Service!") == "custom-team-service"
    assert sanitize_source("🚨") == "unknown"
    assert len(sanitize_source("x" * 80)) == 50


def test_log_extra_normalizes_canonical_scalar_attributes() -> None:
    from core.observability.log_attrs import log_extra

    extra = log_extra({"webhook.source": "Grafana", "drop": None}, **{"request.id": "req-1", "value": {"x": 1}})

    assert extra == {
        "webhook.source": "Grafana",
        "request.id": "req-1",
        "value": "{'x': 1}",
    }


def test_set_span_error_marks_status_and_records_exception(monkeypatch) -> None:
    from core.observability import tracing

    class FakeStatusCode:
        ERROR = "ERROR"

    fake_trace = SimpleNamespace(StatusCode=FakeStatusCode)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    class FakeSpan:
        def __init__(self) -> None:
            self.status: tuple[object, str | None] | None = None
            self.exceptions: list[BaseException] = []

        def set_status(self, status: object, description: str | None = None) -> None:
            self.status = (status, description)

        def record_exception(self, error: BaseException) -> None:
            self.exceptions.append(error)

    span = FakeSpan()
    error = RuntimeError("boom")

    tracing.set_span_error(span, error)

    assert span.status == ("ERROR", "boom")
    assert span.exceptions == [error]


def test_span_ok_and_event_helpers_are_noop_safe(monkeypatch) -> None:
    from core.observability import tracing

    class FakeStatusCode:
        OK = "OK"

    fake_trace = SimpleNamespace(StatusCode=FakeStatusCode)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    class FakeSpan:
        def __init__(self) -> None:
            self.status: tuple[object, str | None] | None = None
            self.events: list[tuple[str, dict[str, object]]] = []

        def set_status(self, status: object, description: str | None = None) -> None:
            self.status = (status, description)

        def add_event(self, name: str, attributes: dict[str, object]) -> None:
            self.events.append((name, attributes))

    span = FakeSpan()

    tracing.set_span_ok(span)
    tracing.add_span_event_to(span, "webhook.pipeline.step.completed", {"pipeline.step": "dedup"})

    assert span.status == ("OK", None)
    assert span.events == [("webhook.pipeline.step.completed", {"pipeline.step": "dedup"})]
