import yaml

from core import json
from tests.helpers.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


def test_grafana_trace_log_correlation_is_bidirectional() -> None:
    datasources = yaml.safe_load((ROOT / "deploy/observability/grafana/provisioning/datasources.yml").read_text())[
        "datasources"
    ]
    by_uid = {item["uid"]: item for item in datasources}

    prometheus = by_uid["prometheus"]["jsonData"]["exemplarTraceIdDestinations"]
    assert {"name": "trace_id", "datasourceUid": "tempo", "urlDisplayLabel": "View Trace"} in prometheus

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


def test_alloy_routes_otlp_logs_without_file_tailing() -> None:
    config = (ROOT / "deploy/observability/alloy/config.alloy").read_text()
    assert 'loki.source.file "webhook_logs"' not in config
    assert 'loki.process "webhook_logs"' not in config
    assert "logs    = [otelcol.processor.memory_limiter.default.input]" in config
    assert "logs    = [otelcol.processor.attributes.loki_labels.input]" in config
    assert (
        'value  = "severity,severity_text,event.name,signal.name,signal.state,webhook.source,webhook.status"' in config
    )

    labels_block = config.split('otelcol.processor.attributes "loki_labels"', 1)[1].split("output", 1)[0]
    assert "trace_id" not in labels_block
    assert "span_id" not in labels_block
    assert "send_exemplars = true" in config


def test_prometheus_loads_webhookwise_rules() -> None:
    prometheus = yaml.safe_load((ROOT / "deploy/observability/prometheus/prometheus.yml").read_text())
    assert "/etc/prometheus/rules/*.yml" in prometheus["rule_files"]
    alerting_targets = prometheus["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert "alertmanager:9093" in alerting_targets

    compose = (ROOT / "deploy/compose/docker-compose.observability.yml").read_text()
    assert "../../deploy/observability/prometheus/alerts.yml:/etc/prometheus/rules/webhookwise-alerts.yml:ro" in compose
    assert "../../deploy/observability/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" in compose
    assert "alertmanager:" in compose

    rules = yaml.safe_load((ROOT / "deploy/observability/prometheus/alerts.yml").read_text())["groups"]
    records = {rule["record"] for group in rules for rule in group["rules"] if "record" in rule}
    alerts = {rule["alert"] for group in rules for rule in group["rules"] if "alert" in rule}
    assert {
        "queue_pending",
        "webhook_events_active",
        "db_pool_connections_checked_out",
        "forward_outbox_oldest_age_seconds",
        "circuit_breaker_active_state",
        "webhookwise:http_request_success_ratio_5m",
        "webhookwise:http_request_error_budget_burn_5m",
        "webhookwise:http_request_error_budget_burn_1h",
        "webhookwise:http_request_error_budget_burn_30m",
        "webhookwise:http_request_error_budget_burn_6h",
        "webhookwise:webhook_ingress_success_ratio_5m",
        "webhookwise:webhook_ingress_error_budget_burn_5m",
        "webhookwise:webhook_ingress_error_budget_burn_1h",
        "webhookwise:webhook_processing_success_ratio_5m",
        "webhookwise:webhook_processing_error_budget_burn_5m",
        "webhookwise:webhook_processing_error_budget_burn_1h",
        "webhookwise:forward_delivery_success_ratio_5m",
        "webhookwise:forward_delivery_error_budget_burn_5m",
        "webhookwise:forward_delivery_error_budget_burn_1h",
        "webhookwise:ai_degradation_ratio_5m",
        "webhookwise:db_pool_utilization_ratio",
        "webhookwise:queue_backlog",
        "webhookwise:redis_unavailable_rate_5m",
    } <= records
    assert {
        "WebhookWiseApiHigh5xxRate",
        "WebhookWiseApiAvailabilitySloBurn",
        "WebhookWiseApiAvailabilityFastBurn",
        "WebhookWiseApiAvailabilitySlowBurn",
        "WebhookWiseIngressSuccessSloBurn",
        "WebhookWiseIngressSuccessFastBurn",
        "WebhookWiseIngressSuccessSlowBurn",
        "WebhookWiseProcessingSuccessSloBurn",
        "WebhookWiseProcessingSuccessFastBurn",
        "WebhookWiseProcessingSuccessSlowBurn",
        "WebhookWiseForwardDeliverySuccessSloBurn",
        "WebhookWiseForwardDeliverySuccessFastBurn",
        "WebhookWiseForwardDeliverySuccessSlowBurn",
        "WebhookWiseAiDegradationHigh",
        "WebhookWiseWebhookDeadLetters",
        "WebhookWiseQueueBacklogHigh",
        "WebhookWiseDbUnhealthy",
        "WebhookWiseRedisUnavailable",
        "WebhookWiseForwardOutboxBacklogOld",
        "WebhookWiseCircuitBreakerOpen",
        "WebhookWiseLokiDrops",
    } <= alerts
    alertmanager = yaml.safe_load((ROOT / "deploy/observability/alertmanager/alertmanager.yml").read_text())
    assert alertmanager["route"]["receiver"] == "webhookwise-local"


def test_tempo_enables_traceql_metrics_generator() -> None:
    tempo = yaml.safe_load((ROOT / "deploy/observability/tempo/tempo.yml").read_text())

    processors = tempo["overrides"]["defaults"]["metrics_generator"]["processors"]
    assert "local-blocks" in processors
    assert tempo["metrics_generator"]["processor"]["local_blocks"]["filter_server_spans"] is False
    assert tempo["metrics_generator"]["storage"]["remote_write"][0]["url"] == "http://prometheus:9090/api/v1/write"
    assert tempo["metrics_generator"]["traces_storage"]["path"]
    assert tempo["query_frontend"]["metrics"]["concurrent_jobs"] <= 8


def test_dashboard_uses_recording_rules_without_raw_metric_fallbacks() -> None:
    dashboard = json.loads((ROOT / "deploy/observability/grafana/dashboards/dashboard.json").read_text())
    diagnostics = json.loads((ROOT / "deploy/observability/grafana/dashboards/dashboard-diagnostics.json").read_text())
    assert dashboard["title"] == "WebhookWise AIOps Overview Dashboard"
    assert diagnostics["title"] == "WebhookWise AIOps Deep Diagnostics Dashboard"
    assert dashboard["timezone"] == "browser"
    assert diagnostics["timezone"] == "browser"
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert "SLO / Alerts / Correlation" in titles
    assert "API Availability SLO 5m" in titles
    assert "SLO Burn Rate (Error Budget)" in titles
    assert "Alert Firing Status (Prometheus Alerts)" in titles
    assert "Webhook / Pipeline Deep Dive" not in titles
    assert "Webhook / Pipeline Deep Dive" in {panel["title"] for panel in diagnostics["panels"]}

    expressions = "\n".join(
        target["expr"]
        for panel in [*dashboard["panels"], *diagnostics["panels"]]
        for target in panel.get("targets", []) or []
        if target.get("expr")
    )
    assert "webhook_events_active" in expressions
    assert "queue_pending_ratio" not in expressions
    assert "db_pool_connections_checked_out_ratio" not in expressions
    assert "forward_outbox_oldest_age_seconds" in expressions
    assert "circuit_breaker_active_state" in expressions
    assert "webhook_suppressed_total" not in expressions
    assert "webhook_noise_evaluations_total" in expressions
    assert "webhookwise:http_request_success_ratio_5m" in expressions
    assert "webhookwise:webhook_processing_success_ratio_5m" in expressions
    assert "webhookwise:forward_delivery_success_ratio_5m" in expressions
    assert "webhookwise:ai_degradation_ratio_5m" in expressions
    assert "webhookwise:http_request_error_budget_burn_5m" in expressions
    assert "webhookwise:webhook_processing_error_budget_burn_1h" in expressions


def test_dashboard_metric_panels_have_trace_and_log_links() -> None:
    dashboards = [
        json.loads((ROOT / "deploy/observability/grafana/dashboards/dashboard.json").read_text()),
        json.loads((ROOT / "deploy/observability/grafana/dashboards/dashboard-diagnostics.json").read_text()),
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
        if {"View related logs", "View related traces"}
        <= {link.get("title") for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])}
    ]
    assert len(metric_panels) >= 64
    assert len(linked_panels) == len(metric_panels)

    link_urls = "\n".join(
        str(link.get("url", ""))
        for panel in metric_panels
        for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])
    )
    assert "${__field.labels.webhook_source}" in link_urls
    assert "${__field.labels.service_name}" in link_urls
    assert "${__from}" in link_urls
    assert "${__to}" in link_urls
    assert "now-1h" not in link_urls
    assert "span.webhook.source" in link_urls
    assert "span.worker.task.name" in link_urls
    assert "span.pipeline.step" in link_urls
    assert "span.forward.target_type" in link_urls
    assert "resource.service.name = " in link_urls
    assert "%26%26" in link_urls

    profile_linked_panels = [
        panel
        for panel in metric_panels
        if "View profile"
        in {link.get("title") for link in panel.get("fieldConfig", {}).get("defaults", {}).get("links", [])}
    ]
    assert len(profile_linked_panels) >= 30
    assert "pyroscope" in link_urls
    assert "profileTypeId" in link_urls


def test_sqlalchemy_shutdown_and_worker_trace_contracts_are_wired() -> None:
    db_engine = (ROOT / "db/engine.py").read_text()
    app = (ROOT / "api/app.py").read_text()
    broker = (ROOT / "core/taskiq_broker.py").read_text()
    webhook = (ROOT / "api/v1/webhook.py").read_text()
    tasks = (ROOT / "services/operations/tasks.py").read_text()
    pipeline_orchestrator = (ROOT / "services/webhooks/pipeline_orchestrator.py").read_text()
    pipeline_runtime = (ROOT / "services/webhooks/pipeline_runtime.py").read_text()
    pipeline_stages = (ROOT / "services/webhooks/pipeline_stages.py").read_text()
    forwarding_stage = (ROOT / "services/webhooks/forwarding_stage.py").read_text()
    forward_outbox = (ROOT / "services/forwarding/outbox.py").read_text()
    redis_metrics = (ROOT / "core/redis_client.py").read_text()
    taskiq_wiring = (ROOT / "services/operations/taskiq_wiring.py").read_text()

    assert "instrument_sqlalchemy(engine.sync_engine)" in db_engine
    assert "shutdown_observability()" in app
    assert "shutdown_observability()" not in broker
    assert "shutdown_observability()" in taskiq_wiring
    assert "services.operations.tasks" not in app
    assert "services.operations.tasks" not in broker
    assert "import services.operations.tasks" in taskiq_wiring
    assert "inject_trace_headers" in webhook
    assert '"traceparent": trace_headers.get("traceparent") or headers.get("traceparent")' in webhook
    assert "trace_context_from_headers(ctx.trace_headers)" in tasks
    assert "inject_trace_headers" in tasks
    assert '"worker.webhook_process_task"' in tasks
    assert '"worker.task.name": "webhook_process_task"' in tasks
    assert "worker.task.status" in tasks
    assert "validate_backpressure(ctx, gate_res)" in pipeline_orchestrator
    assert "resolve_noise_context(ctx, dependencies)" in pipeline_orchestrator
    assert "persist_and_schedule(ctx, analysis, noise, analysis_res, dependencies, gate_res)" in pipeline_orchestrator
    assert '"pipeline.step": step' in pipeline_runtime
    assert 'pipeline_step(ctx, "validate")' in pipeline_stages
    assert 'pipeline_step(ctx, "dedup")' in pipeline_stages
    assert 'pipeline_step(ctx, "noise")' in pipeline_stages
    assert '"webhook.dedup"' in pipeline_runtime
    assert "webhook.pipeline.step.completed" in pipeline_runtime
    assert '"forward.target_type": first_target_type' in forwarding_stage
    assert "FORWARD_TARGET_TYPE" in forward_outbox
    assert '"redis.operation": operation' in redis_metrics


def test_core_runtime_wiring_has_no_service_or_config_side_effects() -> None:
    app = (ROOT / "api/app.py").read_text()
    broker = (ROOT / "core/taskiq_broker.py").read_text()
    forwarding_breakers = (ROOT / "services/forwarding/circuit_breakers.py").read_text()
    entrypoint = (ROOT / "entrypoint.sh").read_text()

    assert "import services." not in broker
    assert "from adapters." not in broker
    assert "from api" not in broker
    assert "services.operations.tasks" not in app
    assert "LazyCircuitBreaker" in forwarding_breakers
    assert "get_default_config" not in forwarding_breakers
    assert "services.operations.taskiq_wiring:broker" in entrypoint
    assert "services.operations.taskiq_wiring:scheduler" in entrypoint


def test_metric_aliases_histogram_views_and_source_limit_are_contractual(monkeypatch) -> None:
    import core.observability.metrics as source_module
    from core.observability.metrics_base import _alias_for_key

    base = (ROOT / "core/observability/metrics_base.py").read_text()
    assert "alias_map" not in base
    assert "_ALIASES" not in (ROOT / "core/observability/attributes.py").read_text()
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
    assert 'export_interval_millis=max(1000, env_int("OTEL_METRIC_EXPORT_INTERVAL", 60000))' in base
    assert 'export_timeout_millis=max(1000, env_int("OTEL_METRIC_EXPORT_TIMEOUT", 30000))' in base
    assert "context=context.get_current()" in base

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

    scanner = (ROOT / "services/forwarding/outbox_scanner.py").read_text()
    breaker = (ROOT / "core/circuit_breaker.py").read_text()
    assert "FORWARD_OUTBOX_BACKLOG_AGE_SECONDS" in scanner
    assert "CIRCUIT_BREAKER_STATE" in breaker
    assert "_record_state_metric" in breaker


def test_otlp_signals_are_explicit_and_logs_use_otlp_by_default() -> None:
    exporters = (ROOT / "core/observability/exporters.py").read_text()
    logging_py = (ROOT / "core/observability/logging.py").read_text()
    env_example = (ROOT / ".env.example.all").read_text()
    app_compose = (ROOT / "deploy/compose/docker-compose.yml").read_text()
    observability_compose = (ROOT / "deploy/compose/docker-compose.observability.yml").read_text()

    assert 'return env_flag("OTEL_ENABLED", default=False)' in exporters
    assert (
        "OTEL_EXPORTER_OTLP_ENDPOINT" not in exporters.split("def otel_enabled", 1)[1].split("def parse_headers", 1)[0]
    )
    assert 'env_flag("OTEL_LOGS_ENABLED", default=False)' in logging_py
    assert "OTEL_LOGS_ENABLED=true" in env_example
    assert "OTEL_LOGS_ENABLED: ${OTEL_LOGS_ENABLED:-false}" in app_compose
    assert "OTEL_SERVICE_NAMESPACE=webhookwise" in env_example
    assert "OTEL_SEMCONV_VERSION=1.41.0" in env_example
    assert "OTEL_SCHEMA_URL=https://opentelemetry.io/schemas/1.41.0" in env_example
    assert "OTEL_METRICS_EXEMPLAR_FILTER=trace_based" in env_example
    assert "OTEL_METRICS_EXEMPLAR_FILTER: ${OTEL_METRICS_EXEMPLAR_FILTER:-trace_based}" in app_compose
    assert "--enable-feature=native-histograms,exemplar-storage" in observability_compose


def test_otel_schema_scope_and_structured_log_helpers_are_wired() -> None:
    attributes = (ROOT / "core/observability/attributes.py").read_text()
    resource = (ROOT / "core/observability/resource.py").read_text()
    tracing = (ROOT / "core/observability/tracing.py").read_text()
    metrics_base = (ROOT / "core/observability/metrics_base.py").read_text()
    logger = (ROOT / "core/logger.py").read_text()
    middleware = (ROOT / "core/web/middleware.py").read_text()
    events = (ROOT / "core/observability/events.py").read_text()

    assert 'OTEL_SEMCONV_VERSION_DEFAULT = "1.41.0"' in attributes
    assert "schema_url=get_otel_schema_url()" in resource
    assert "INSTRUMENTATION_SCOPE_NAME" in tracing
    assert "schema_url=get_otel_schema_url()" in tracing
    assert "schema_url=get_otel_schema_url()" in metrics_base
    assert "sampler=_build_sampler()" in tracing
    assert "parentbased_traceidratio" in tracing
    assert '"schema_url": get_otel_schema_url()' in logger
    assert "from core.observability.log_attrs import log_extra" in middleware
    assert "extra=log_extra(" in middleware
    assert "extra=log_extra(normalized)" in events


def test_observability_cli_exposes_smoke_and_tempo_commands() -> None:
    cli = (ROOT / "scripts/observability/webhookwise_observe.py").read_text()
    mcp = (ROOT / "scripts/observability/webhookwise_mcp.py").read_text()
    dashboard_links = (ROOT / "scripts/observability/update_dashboard_links.py").read_text()
    assert 'sub.add_parser("smoke"' in cli
    assert 'sub.add_parser("tempo"' in cli
    assert 'sub.add_parser("profiles"' in cli
    assert 'sub.add_parser("acceptance"' in cli
    assert 'sub.add_parser("contract"' in cli
    assert 'sub.add_parser("runbook"' in cli
    assert '"name": "webhookwise_smoke"' in mcp
    assert '"name": "webhookwise_tempo_search"' in mcp
    assert '"name": "webhookwise_profiles"' in mcp
    assert '"name": "webhookwise_acceptance"' in mcp
    assert '"name": "webhookwise_contract"' in mcp
    assert '"name": "webhookwise_runbook"' in mcp
    assert "PROFILE_TYPE_ID" in dashboard_links


def test_offline_telemetry_contract_passes() -> None:
    from scripts.observability.query_lib import telemetry_contract

    rows = telemetry_contract(ROOT)
    assert all(row["status"] == "ok" for row in rows), rows


def test_docs_and_e2e_cover_project_operability_contracts() -> None:
    readme = (ROOT / "README.md").read_text()
    docs_readme = (ROOT / "docs/README.md").read_text()
    e2e_compose = (ROOT / "tests/e2e/docker-compose.yml").read_text()
    e2e_runner = (ROOT / "tests/e2e/run_webhook_to_feishu.sh").read_text()

    assert (ROOT / "CONTRIBUTING.md").exists()
    assert (ROOT / "CHANGELOG.md").exists()
    assert "docs/reference/api.md" in readme
    assert "CONTRIBUTING.md" in readme
    assert "CHANGELOG.md" in readme
    assert "reference/api.md" in docs_readme
    assert "fake-openai" in e2e_compose
    assert 'ENABLE_AI_ANALYSIS: "true"' in e2e_compose
    assert 'CACHE_ENABLED: "true"' in e2e_compose
    assert 'ENABLE_ALERT_NOISE_REDUCTION: "true"' in e2e_compose
    assert "/v1/forward-rules" in e2e_runner
    assert "e2e-admin-write-key" in e2e_runner
    assert "http://open.feishu.cn:9000/open-apis/bot/v2/hook/e2e-token" in e2e_runner
    assert "AI E2E 摘要" in e2e_runner
    assert "/v1/chat/completions" in e2e_runner
