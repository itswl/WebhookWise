import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_grafana_trace_log_correlation_is_bidirectional() -> None:
    datasources = yaml.safe_load((ROOT / "deploy/observability/grafana-datasources.yml").read_text())["datasources"]
    by_uid = {item["uid"]: item for item in datasources}

    tempo = by_uid["tempo"]["jsonData"]["tracesToLogsV2"]
    assert tempo["datasourceUid"] == "loki"
    assert tempo["filterByTraceID"] is True
    assert tempo["filterBySpanID"] is False
    assert {"key": "service.name", "value": "service_name"} in tempo["tags"]
    assert {"key": "webhook.source", "value": "webhook_source"} in tempo["tags"]

    loki = by_uid["loki"]["jsonData"]["derivedFields"]
    trace_link = next(item for item in loki if item["name"] == "TraceID")
    assert trace_link["datasourceUid"] == "tempo"
    assert "trace_id" in trace_link["matcherRegex"]
    assert trace_link["url"] == "$${__value.raw}"


def test_alloy_extracts_trace_fields_without_labeling_trace_id() -> None:
    config = (ROOT / "deploy/observability/alloy.alloy").read_text()
    assert 'trace_id       = "trace_id"' in config
    assert 'span_id        = "span_id"' in config
    assert 'webhook_source = "source"' in config
    assert 'webhook_status = "processing_status"' in config
    assert 'event_name             = "event_name"' in config

    labels_block = config.split("stage.labels {", 1)[1].split("forward_to", 1)[0]
    assert "trace_id" not in labels_block
    assert "span_id" not in labels_block


def test_prometheus_loads_webhookwise_rules() -> None:
    prometheus = yaml.safe_load((ROOT / "deploy/observability/prometheus.yml").read_text())
    assert "/etc/prometheus/rules/*.yml" in prometheus["rule_files"]
    alerting_targets = prometheus["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert "alertmanager:9093" in alerting_targets

    compose = (ROOT / "docker-compose.observability.yml").read_text()
    assert "./deploy/observability/alerts.yml:/etc/prometheus/rules/webhookwise-alerts.yml:ro" in compose
    assert "./deploy/observability/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" in compose
    assert "alertmanager:" in compose

    rules = yaml.safe_load((ROOT / "deploy/observability/alerts.yml").read_text())["groups"]
    records = {rule["record"] for group in rules for rule in group["rules"] if "record" in rule}
    alerts = {rule["alert"] for group in rules for rule in group["rules"] if "alert" in rule}
    assert {
        "queue_pending",
        "webhook_events_active",
        "db_pool_connections_checked_out",
        "forward_outbox_oldest_age_seconds",
        "circuit_breaker_active_state",
    } <= records
    assert {
        "WebhookWiseApiHigh5xxRate",
        "WebhookWiseWebhookDeadLetters",
        "WebhookWiseQueueBacklogHigh",
        "WebhookWiseForwardOutboxBacklogOld",
        "WebhookWiseCircuitBreakerOpen",
        "WebhookWiseLokiDrops",
    } <= alerts
    alertmanager = yaml.safe_load((ROOT / "deploy/observability/alertmanager.yml").read_text())
    assert alertmanager["route"]["receiver"] == "webhookwise-local"


def test_dashboard_includes_slo_and_recording_rule_fallbacks() -> None:
    dashboard = json.loads((ROOT / "grafana/dashboard.json").read_text())
    diagnostics = json.loads((ROOT / "grafana/dashboard-diagnostics.json").read_text())
    assert dashboard["title"] == "WebhookWise AIOps 基础大盘"
    assert diagnostics["title"] == "WebhookWise AIOps 深度诊断大盘"
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert "SLO、告警与链路闭环 (SLO / Alerts / Correlation)" in titles
    assert "API 可用性 SLO 1h" in titles
    assert "告警触发状态 (Prometheus Alerts)" in titles
    assert "Webhook 与 Pipeline 深度诊断 (Webhook / Pipeline Deep Dive)" not in titles
    assert "Webhook 与 Pipeline 深度诊断 (Webhook / Pipeline Deep Dive)" in {
        panel["title"] for panel in diagnostics["panels"]
    }

    expressions = "\n".join(
        target["expr"]
        for panel in [*dashboard["panels"], *diagnostics["panels"]]
        for target in panel.get("targets", []) or []
        if target.get("expr")
    )
    assert "webhook_events_active" in expressions
    assert "queue_pending) or max(queue_pending_ratio)" in expressions
    assert "db_pool_connections_checked_out) or max(db_pool_connections_checked_out_ratio)" in expressions
    assert "forward_outbox_oldest_age_seconds" in expressions
    assert "circuit_breaker_active_state" in expressions


def test_dashboard_metric_panels_have_trace_and_log_links() -> None:
    dashboards = [
        json.loads((ROOT / "grafana/dashboard.json").read_text()),
        json.loads((ROOT / "grafana/dashboard-diagnostics.json").read_text()),
    ]
    metric_panels = [
        panel
        for dashboard in dashboards
        for panel in dashboard["panels"]
        if panel.get("type") != "row" and any(target.get("expr") for target in panel.get("targets", []) or [])
    ]
    linked_panels = [
        panel
        for panel in metric_panels
        if {"查看相关日志", "查看相关 Trace"}
        <= {link.get("title") for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])}
    ]
    assert len(metric_panels) >= 64
    assert len(linked_panels) == len(metric_panels)


def test_sqlalchemy_shutdown_and_worker_trace_contracts_are_wired() -> None:
    db_session = (ROOT / "db/session.py").read_text()
    app = (ROOT / "core/app.py").read_text()
    broker = (ROOT / "core/taskiq_broker.py").read_text()
    webhook = (ROOT / "api/webhook.py").read_text()
    tasks = (ROOT / "services/operations/tasks.py").read_text()

    assert "instrument_sqlalchemy(_engine.sync_engine)" in db_session
    assert "shutdown_observability()" in app
    assert "shutdown_observability()" in broker
    assert '"traceparent": headers.get("traceparent") or build_traceparent(request_id)' in webhook
    assert "trace_context_from_headers(trace_headers)" in tasks
    assert '"worker.webhook_process_task"' in tasks


def test_metric_aliases_histogram_views_and_source_limit_are_contractual(monkeypatch) -> None:
    import core.observability.metrics.source as source_module
    from core.observability.metrics.base import _alias_for_key

    base = (ROOT / "core/observability/metrics/base.py").read_text()
    attributes = (ROOT / "core/observability/attributes.py").read_text()
    assert "alias_map" not in base
    assert "_ALIASES" in attributes
    assert _alias_for_key("source", ("webhook.source",)) == "webhook.source"
    assert _alias_for_key("token_type", ("ai.token_type",)) == "ai.token_type"

    for instrument in (
        "http.server.request.duration",
        "webhook.processing.duration",
        "webhook.ingress.payload.size",
        "ai.request.duration",
        "db.session.duration",
        "queue.operation.duration",
        "forward.outbox.process.duration",
    ):
        assert instrument in base

    monkeypatch.setattr(source_module, "_SOURCE_LABEL_LIMIT", 2)
    source_module._reset_source_label_cache_for_tests()
    try:
        assert source_module.sanitize_source("alpha") == "alpha"
        assert source_module.sanitize_source("beta") == "beta"
        assert source_module.sanitize_source("gamma") == "other"
    finally:
        source_module._reset_source_label_cache_for_tests()


def test_resilience_and_outbox_operational_metrics_exist() -> None:
    from core.observability import metrics

    assert metrics.CIRCUIT_BREAKER_STATE.label_keys == ("circuit_breaker.name", "circuit_breaker.state")
    assert metrics.FORWARD_OUTBOX_BACKLOG_AGE_SECONDS.label_keys == ("forward.target_type", "forward.status")

    outbox = (ROOT / "services/forwarding/outbox.py").read_text()
    breaker = (ROOT / "core/circuit_breaker.py").read_text()
    assert "FORWARD_OUTBOX_BACKLOG_AGE_SECONDS" in outbox
    assert "CIRCUIT_BREAKER_STATE" in breaker
    assert "_record_state_metric" in breaker


def test_otlp_logs_are_opt_in_and_otel_enabled_is_explicit() -> None:
    exporters = (ROOT / "core/observability/exporters.py").read_text()
    logging_py = (ROOT / "core/observability/logging.py").read_text()
    env_example = (ROOT / ".env.example").read_text()

    assert 'return env_flag("OTEL_ENABLED", default=False)' in exporters
    assert (
        "OTEL_EXPORTER_OTLP_ENDPOINT" not in exporters.split("def otel_enabled", 1)[1].split("def parse_headers", 1)[0]
    )
    assert 'env_flag("OTEL_LOGS_ENABLED", default=False)' in logging_py
    assert "OTEL_LOGS_ENABLED=false" in env_example


def test_observability_cli_exposes_smoke_and_tempo_commands() -> None:
    cli = (ROOT / "scripts/observability/webhookwise_observe.py").read_text()
    mcp = (ROOT / "scripts/observability/webhookwise_mcp.py").read_text()
    assert 'sub.add_parser("smoke"' in cli
    assert 'sub.add_parser("tempo"' in cli
    assert '"name": "webhookwise_smoke"' in mcp
    assert '"name": "webhookwise_tempo_search"' in mcp
