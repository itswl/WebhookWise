from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any


def test_emit_event_records_metric_span_event_and_structured_log(monkeypatch) -> None:
    from core.observability import events

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
        events.emit_event("webhook.test", {"source": "github"}, body="test event")
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)

    assert metric_names == ["webhook.test"]
    assert span_events == [("webhook.test", {"webhook.source": "github", "event.name": "webhook.test"})]
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
        "HTTP_SERVER_REQUESTS_TOTAL": ("http.method", "http.route", "http.status_code"),
        "HTTP_SERVER_REQUEST_DURATION_SECONDS": ("http.method", "http.route", "http.status_code"),
        "HTTP_SERVER_REQUEST_BODY_BYTES": ("http.method", "http.route"),
        "SECURITY_CHECKS_TOTAL": ("security.check", "security.result"),
        "WEBHOOK_INGRESS_PAYLOAD_BYTES": ("webhook.source", "webhook.outcome"),
        "WEBHOOK_PIPELINE_STEP_TOTAL": ("pipeline.step", "webhook.source", "webhook.outcome"),
        "WEBHOOK_PIPELINE_STEP_DURATION_SECONDS": ("pipeline.step", "webhook.source", "webhook.outcome"),
        "WEBHOOK_NOISE_EVALUATIONS_TOTAL": ("webhook.source", "webhook.relation", "webhook.suppressed"),
        "WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS": ("webhook.source", "webhook.relation", "webhook.suppressed"),
        "AI_CACHE_REQUESTS_TOTAL": ("ai.cache.operation", "ai.cache.result"),
        "AI_CACHE_OPERATION_DURATION_SECONDS": ("ai.cache.operation", "ai.cache.result"),
        "AI_DEGRADATIONS_TOTAL": ("ai.degradation.reason",),
        "WORKER_TASKS_TOTAL": ("worker.task.name", "worker.task.status"),
        "WORKER_TASK_DURATION_SECONDS": ("worker.task.name", "worker.task.status"),
        "FORWARD_DELIVERY_TOTAL": ("forward.target_type", "forward.status"),
        "FORWARD_DELIVERY_DURATION_SECONDS": ("forward.target_type", "forward.status"),
        "FORWARD_OUTBOX_RECORDS_TOTAL": ("forward.target_type", "forward.status"),
        "FORWARD_OUTBOX_PROCESS_DURATION_SECONDS": ("forward.target_type", "forward.status"),
        "QUEUE_OPERATIONS_TOTAL": ("queue.name", "queue.operation", "queue.status"),
        "QUEUE_OPERATION_DURATION_SECONDS": ("queue.name", "queue.operation", "queue.status"),
        "DB_SESSION_TOTAL": ("db.operation", "db.status"),
        "DB_SESSION_DURATION_SECONDS": ("db.operation", "db.status"),
        "REDIS_OPERATIONS_TOTAL": ("redis.operation", "redis.status"),
        "REDIS_OPERATION_DURATION_SECONDS": ("redis.operation", "redis.status"),
    }

    forbidden_labels = {"webhook.event_id", "webhook.alert_hash", "forward.target", "url", "request_id"}
    for metric_name, label_keys in expected_label_keys.items():
        metric = getattr(metrics, metric_name)
        assert metric.label_keys == label_keys
        assert forbidden_labels.isdisjoint(metric.label_keys)


def test_component_metrics_are_exported_from_legacy_facade() -> None:
    import core.metrics as facade
    from core.observability import metrics

    metric_names = [
        "HTTP_SERVER_REQUESTS_TOTAL",
        "SECURITY_CHECKS_TOTAL",
        "WEBHOOK_INGRESS_PAYLOAD_BYTES",
        "WEBHOOK_PIPELINE_STEP_TOTAL",
        "AI_CACHE_REQUESTS_TOTAL",
        "AI_DEGRADATIONS_TOTAL",
        "WORKER_TASKS_TOTAL",
        "FORWARD_DELIVERY_TOTAL",
        "FORWARD_OUTBOX_RECORDS_TOTAL",
        "QUEUE_OPERATIONS_TOTAL",
        "DB_SESSION_TOTAL",
        "REDIS_OPERATIONS_TOTAL",
    ]
    for metric_name in metric_names:
        assert getattr(facade, metric_name) is getattr(metrics, metric_name)
