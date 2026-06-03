"""Shared helpers for querying the local WebhookWise observability stack.

The module intentionally uses only the Python standard library so it can run in
the app container, on a developer laptop, or from a lightweight MCP wrapper.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core import json

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_USER_AGENT = "WebhookWise-Observability/0.1"


@dataclass(frozen=True)
class Endpoints:
    query_mode: str = "direct"
    prometheus: str = "http://localhost:9090"
    loki: str = "http://localhost:3100"
    tempo: str = "http://localhost:3200"
    grafana: str = "http://localhost:3000"
    pyroscope: str = "http://localhost:4040"
    alloy: str = "http://localhost:12345"
    api: str = "http://localhost:8000"
    grafana_user: str = "admin"
    grafana_password: str = "admin"
    grafana_token: str = ""
    prometheus_datasource_uid: str = "prometheus"
    loki_datasource_uid: str = "loki"
    tempo_datasource_uid: str = "tempo"
    pyroscope_datasource_uid: str = "pyroscope"

    @classmethod
    def from_env(cls) -> Endpoints:
        return cls(
            query_mode=os.getenv("WEBHOOKWISE_QUERY_MODE", cls.query_mode),
            prometheus=os.getenv("WEBHOOKWISE_PROMETHEUS_URL", cls.prometheus).rstrip("/"),
            loki=os.getenv("WEBHOOKWISE_LOKI_URL", cls.loki).rstrip("/"),
            tempo=os.getenv("WEBHOOKWISE_TEMPO_URL", cls.tempo).rstrip("/"),
            grafana=os.getenv("WEBHOOKWISE_GRAFANA_URL", cls.grafana).rstrip("/"),
            pyroscope=os.getenv("WEBHOOKWISE_PYROSCOPE_URL", cls.pyroscope).rstrip("/"),
            alloy=os.getenv("WEBHOOKWISE_ALLOY_URL", cls.alloy).rstrip("/"),
            api=os.getenv("WEBHOOKWISE_API_URL", cls.api).rstrip("/"),
            grafana_user=os.getenv("WEBHOOKWISE_GRAFANA_USER", cls.grafana_user),
            grafana_password=os.getenv("WEBHOOKWISE_GRAFANA_PASSWORD", cls.grafana_password),
            grafana_token=os.getenv("WEBHOOKWISE_GRAFANA_TOKEN", cls.grafana_token),
            prometheus_datasource_uid=os.getenv(
                "WEBHOOKWISE_PROMETHEUS_DATASOURCE_UID",
                cls.prometheus_datasource_uid,
            ),
            loki_datasource_uid=os.getenv("WEBHOOKWISE_LOKI_DATASOURCE_UID", cls.loki_datasource_uid),
            tempo_datasource_uid=os.getenv("WEBHOOKWISE_TEMPO_DATASOURCE_UID", cls.tempo_datasource_uid),
            pyroscope_datasource_uid=os.getenv("WEBHOOKWISE_PYROSCOPE_DATASOURCE_UID", cls.pyroscope_datasource_uid),
        )


PROMQL_PRESETS: dict[str, str] = {
    "api-rate": 'sum by (http_route, http_response_status_code) (rate(http_server_request_duration_seconds_count{service_name="webhookwise-api"}[5m]))',
    "api-latency-p95": (
        "histogram_quantile(0.95, sum by (le, http_route) "
        '(rate(http_server_request_duration_seconds_bucket{service_name="webhookwise-api"}[5m])))'
    ),
    "api-5xx-rate": (
        '100 * ((sum(rate(http_server_request_duration_seconds_count{service_name="webhookwise-api", http_response_status_code=~"5.."}[5m])) '
        'or vector(0)) / clamp_min((sum(rate(http_server_request_duration_seconds_count{service_name="webhookwise-api"}[5m])) '
        "or vector(0)), 0.000001))"
    ),
    "webhook-rate": "sum by (webhook_source) (rate(webhook_received_total[5m]))",
    "active-events": "max(webhook_events_active) or vector(0)",
    "queue-backlog": "max(queue_pending) or max(queue_lag) or vector(0)",
    "queue-retained-depth": "max by (queue_stream) (queue_depth) or vector(0)",
    "queue-ops": "sum by (queue_operation, queue_status) (rate(queue_operations_total[5m]))",
    "worker-runs": "sum by (worker_task_name, worker_task_status) (rate(worker_task_runs_total[5m]))",
    "worker-latency-p95": (
        "histogram_quantile(0.95, sum by (le, worker_task_name) (rate(worker_task_duration_seconds_bucket[5m])))"
    ),
    "db-pool": "max(db_pool_connections_checked_out) or max(db_pool_connections_max) or vector(0)",
    "db-latency-p95": (
        "histogram_quantile(0.95, sum by (le, db_operation) (rate(db_session_duration_seconds_bucket[5m])))"
    ),
    "redis-latency-p95": (
        "histogram_quantile(0.95, sum by (le, redis_operation) (rate(redis_operation_duration_seconds_bucket[5m])))"
    ),
    "scheduler-lag": "max by (scheduler_task_name) (scheduler_task_lag_seconds) or vector(0)",
    "scheduler-last-success-age": "time() - max by (scheduler_task_name) (scheduler_task_last_success_unixtime_seconds)",
    "noise-rate": "sum by (webhook_relation, webhook_suppressed) (rate(webhook_noise_evaluations_total[5m]))",
    "suppression-rate": (
        '100 * ((sum(rate(webhook_noise_evaluations_total{webhook_suppressed="true"}[5m])) or vector(0)) / '
        "clamp_min((sum(rate(webhook_noise_evaluations_total[5m])) or vector(0)), 0.000001))"
    ),
    "slo-api-success": "webhookwise:http_request_success_ratio_5m",
    "slo-ingress-success": "webhookwise:webhook_ingress_success_ratio_5m",
    "slo-processing-success": "webhookwise:webhook_processing_success_ratio_5m",
    "slo-forward-success": "webhookwise:forward_delivery_success_ratio_5m",
    "slo-ai-degradation": "webhookwise:ai_degradation_ratio_5m",
    "slo-db-utilization": "webhookwise:db_pool_utilization_ratio",
    "slo-queue-backlog": "webhookwise:queue_backlog",
    "ai-latency-p95": (
        "histogram_quantile(0.95, sum by (le, ai_engine) (rate(ai_request_duration_seconds_bucket[5m])))"
    ),
    "ai-cost": "sum(ai_cost_USD_total) or vector(0)",
    "ai-tokens": "sum by (ai_model, ai_token_type) (increase(ai_tokens_total[6h])) or vector(0)",
    "ai-cache-rate": ("sum by (ai_cache_operation, ai_cache_result) (rate(ai_cache_requests_total[5m])) or vector(0)"),
    "ai-cache-latency-p95": (
        "histogram_quantile(0.95, sum by (le, ai_cache_operation, ai_cache_result) "
        "(rate(ai_cache_operation_duration_seconds_bucket[5m])))"
    ),
    "deep-analysis-rate": ("sum by (webhook_status, ai_engine) (rate(ai_deep_analysis_total[5m])) or vector(0)"),
    "forward-rate": "sum by (forward_target_type, forward_status) (rate(forward_delivery_total[5m]))",
    "forward-outbox-rate": (
        "sum by (forward_target_type, forward_status) (rate(forward_outbox_records_total[5m])) or vector(0)"
    ),
    "forward-outbox-latency-p95": (
        "histogram_quantile(0.95, sum by (le, forward_target_type) "
        "(rate(forward_outbox_process_duration_seconds_bucket[5m])))"
    ),
    "forward-outbox-backlog-age": (
        "max by (forward_target_type, forward_status) (forward_outbox_oldest_age_seconds) or vector(0)"
    ),
    "circuit-breaker-state": (
        "max by (circuit_breaker_name, circuit_breaker_state) (circuit_breaker_active_state) or vector(0)"
    ),
    "webhook-status": "max by (webhook_status) (webhook_processing_status_count) or vector(0)",
    "pipeline-step-latency-p95": (
        "histogram_quantile(0.95, sum by (le, pipeline_step) (rate(webhook_pipeline_step_duration_seconds_bucket[5m])))"
    ),
    "queue-operation-latency-p95": (
        "histogram_quantile(0.95, sum by (le, queue_operation) (rate(queue_operation_duration_seconds_bucket[5m])))"
    ),
    "webhook-payload-p95": (
        "histogram_quantile(0.95, sum by (le, webhook_source) (rate(webhook_ingress_payload_size_bytes_bucket[5m])))"
    ),
    "noise-evaluations": (
        "sum by (webhook_source, webhook_relation, webhook_suppressed) "
        "(rate(webhook_noise_evaluations_total[5m])) or vector(0)"
    ),
    "noise-latency-p95": (
        "histogram_quantile(0.95, sum by (le, webhook_relation) "
        "(rate(webhook_noise_evaluation_duration_seconds_bucket[5m])))"
    ),
    "faro-rum": (
        "sum(rate(faro_receiver_events_total[5m])) or vector(0) or sum(rate(faro_receiver_measurements_total[5m]))"
    ),
    "beyla-calls": (
        'sum by (span_name, span_kind) (rate(traces_span_metrics_calls_total{source="beyla", '
        'service_name="webhookwise-api"}[5m]))'
    ),
    "k6-smoke": (
        "max_over_time(k6_http_req_duration_p95[6h]) or vector(0) or "
        "max_over_time(k6_http_req_failed_rate[6h]) or max_over_time(k6_checks_rate[6h])"
    ),
    "collector-health": (
        "alloy_config_last_load_successful or alloy_component_controller_running_components or "
        "increase(loki_write_dropped_entries_total[6h])"
    ),
    "environment-services": (
        'count by (deployment_environment, service_name, job) ({__name__=~"http_server_request_duration_seconds_count|'
        'worker_task_runs_total|scheduler_task_runs_total|ai_tokens_total|process_cpu_utilization_ratio"})'
    ),
    "process-memory": "sum by (service_name) (process_memory_usage_bytes) or vector(0)",
    "service-graph-rate": (
        "sum by (client, server, connection_type) (rate(traces_service_graph_request_total[5m])) or vector(0)"
    ),
    "service-graph-failures": (
        "sum by (client, server, connection_type) (rate(traces_service_graph_request_failed_total[5m])) or vector(0)"
    ),
    "collector-queue": "otelcol_exporter_queue_size or vector(0)",
    "loki-write-latency-p95": (
        "histogram_quantile(0.95, sum by (le) (rate(loki_write_request_duration_seconds_bucket[5m])))"
    ),
    "loki-write-retries": (
        "sum(rate(loki_write_batch_retries_total[5m])) or vector(0) or "
        "sum(rate(loki_write_dropped_entries_total[5m])) or sum(rate(loki_write_dropped_bytes_total[5m]))"
    ),
}

_PROMQL_METRIC_TOKEN_RE = re.compile(
    r"\b(?:webhookwise:[a-zA-Z0-9_:]+|[a-zA-Z_:][a-zA-Z0-9_:]*(?::[a-zA-Z0-9_:]+)?"
    r"(?:_total|_count|_sum|_bucket|_ratio|_seconds|_bytes|_age|_pending|_lag|_depth|_state|"
    r"_active|_max|_out|_unixtime|_checks_rate|_duration_p95|_duration_p99|_failed_rate|"
    r"_http_reqs_total|_vus|_sent_total|_received_total|_load_successful|_backlog))\b"
)
_OTEL_METRIC_RE = re.compile(r"=\s*(Counter|Gauge|Histogram)\(\s*\n?\s*\"([a-zA-Z0-9_.]+)\"", re.MULTILINE)
_RECORDING_RULE_RE = re.compile(r"^\s*-\s*record:\s*([^\s]+)\s*$", re.MULTILINE)
_QUOTED_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_EXTERNAL_METRIC_PREFIXES = (
    "http_",
    "process_",
    "traces_",
    "faro_",
    "k6_",
    "alloy_",
    "loki_",
    "otelcol_",
    "prometheus_",
)
_EXTERNAL_METRICS = {"ALERTS", "up"}
_PROMQL_FUNCTIONS = {
    "abs",
    "avg",
    "avg_over_time",
    "ceil",
    "clamp_max",
    "clamp_min",
    "count",
    "count_over_time",
    "delta",
    "histogram_quantile",
    "increase",
    "label_replace",
    "last_over_time",
    "max",
    "max_over_time",
    "min",
    "or",
    "quantile",
    "rate",
    "round",
    "scalar",
    "sort",
    "sum",
    "time",
    "topk",
    "vector",
}
_HIGH_CARDINALITY_LOKI_LABELS = {
    "trace_id",
    "span_id",
    "request.id",
    "request_id",
    "webhook.event_id",
    "webhook.alert_hash",
    "forward.target",
    "forward.target_url",
    "url",
    "http.url",
}
_OLD_TELEMETRY_NAMES = {
    "webhook_suppressed_total",
    "request_id",
    "route_type",
}


def _request_json(
    url: str,
    *,
    params: dict[str, str] | None = None,
    method: str = "GET",
    basic_auth: tuple[str, str] | None = None,
    bearer_token: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if params:
        encoded = urllib.parse.urlencode(params)
        if method == "GET":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{encoded}"
            data = None
        else:
            data = encoded.encode()
    else:
        data = None

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("User-Agent", os.getenv("WEBHOOKWISE_HTTP_USER_AGENT", DEFAULT_USER_AGENT))
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    if bearer_token:
        request.add_header("Authorization", f"Bearer {bearer_token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} failed: {exc}") from exc

    try:
        return cast(dict[str, Any], json.loads(body))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} returned non-JSON response: {body[:500]}") from exc


def _request_text(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    basic_auth: tuple[str, str] | None = None,
    bearer_token: str | None = None,
) -> str:
    request = urllib.request.Request(url)
    request.add_header("User-Agent", os.getenv("WEBHOOKWISE_HTTP_USER_AGENT", DEFAULT_USER_AGENT))
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    if bearer_token:
        request.add_header("Authorization", f"Bearer {bearer_token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(response.read().decode(errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} failed: {exc}") from exc


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = json.dumps_bytes(payload)
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("User-Agent", os.getenv("WEBHOOKWISE_HTTP_USER_AGENT", DEFAULT_USER_AGENT))
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {response_body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} failed: {exc}") from exc
    try:
        return cast(dict[str, Any], json.loads(response_body))
    except json.JSONDecodeError:
        return {"raw": response_body}


def prometheus_query(query: str, endpoints: Endpoints | None = None) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    if endpoints.query_mode == "grafana-proxy":
        return _request_json(
            f"{endpoints.grafana}/api/datasources/proxy/uid/{endpoints.prometheus_datasource_uid}/api/v1/query",
            method="POST",
            params={"query": query},
            **_grafana_auth(endpoints),
        )
    return _request_json(
        f"{endpoints.prometheus}/api/v1/query",
        method="POST",
        params={"query": query},
    )


def prometheus_series(match: str, endpoints: Endpoints | None = None) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    if endpoints.query_mode == "grafana-proxy":
        return _request_json(
            f"{endpoints.grafana}/api/datasources/proxy/uid/{endpoints.prometheus_datasource_uid}/api/v1/series",
            method="POST",
            params={"match[]": match},
            **_grafana_auth(endpoints),
        )
    return _request_json(
        f"{endpoints.prometheus}/api/v1/series",
        method="POST",
        params={"match[]": match},
    )


def loki_query_range(
    query: str,
    endpoints: Endpoints | None = None,
    *,
    limit: int = 20,
    since_seconds: int = 3600,
) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - since_seconds * 1_000_000_000
    if endpoints.query_mode == "grafana-proxy":
        return _request_json(
            f"{endpoints.grafana}/api/datasources/proxy/uid/{endpoints.loki_datasource_uid}/loki/api/v1/query_range",
            params={
                "query": query,
                "limit": str(limit),
                "direction": "backward",
                "start": str(start_ns),
                "end": str(end_ns),
            },
            **_grafana_auth(endpoints),
        )
    return _request_json(
        f"{endpoints.loki}/loki/api/v1/query_range",
        params={
            "query": query,
            "limit": str(limit),
            "direction": "backward",
            "start": str(start_ns),
            "end": str(end_ns),
        },
    )


def tempo_search(
    endpoints: Endpoints | None = None,
    *,
    service_name: str = "webhookwise-api",
    limit: int = 1,
) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    params = {"tags": f'service.name="{service_name}"', "limit": str(limit)}
    if endpoints.query_mode == "grafana-proxy":
        return _request_json(
            f"{endpoints.grafana}/api/datasources/proxy/uid/{endpoints.tempo_datasource_uid}/api/search",
            params=params,
            **_grafana_auth(endpoints),
        )
    return _request_json(f"{endpoints.tempo}/api/search", params=params)


def profile_selector(service_name: str, *, profile_type_id: str = "process_cpu:cpu:nanoseconds:cpu:nanoseconds") -> str:
    service = service_name.strip() or "webhookwise-api"
    return f'{{service_name="{service}", profile_type="{profile_type_id}"}}'


def grafana_profile_url(
    service_name: str,
    endpoints: Endpoints | None = None,
    *,
    from_expr: str = "now-1h",
    to_expr: str = "now",
    relative: bool = False,
    profile_type_id: str = "process_cpu:cpu:nanoseconds:cpu:nanoseconds",
) -> str:
    endpoints = endpoints or Endpoints.from_env()
    left = {
        "datasource": endpoints.pyroscope_datasource_uid,
        "queries": [
            {
                "refId": "A",
                "query": profile_selector(service_name, profile_type_id=profile_type_id),
                "queryType": "profile",
                "profileTypeId": profile_type_id,
            }
        ],
        "range": {"from": from_expr, "to": to_expr},
    }
    state = urllib.parse.quote(json.dumps(left), safe="")
    path = f"/explore?orgId=1&left={state}"
    if relative:
        return path
    return f"{endpoints.grafana}{path}"


def pyroscope_profile_url(
    service_name: str,
    endpoints: Endpoints | None = None,
    *,
    from_expr: str = "now-1h",
    to_expr: str = "now",
    profile_type_id: str = "process_cpu:cpu:nanoseconds:cpu:nanoseconds",
) -> str:
    endpoints = endpoints or Endpoints.from_env()
    params = urllib.parse.urlencode(
        {
            "query": profile_selector(service_name, profile_type_id=profile_type_id),
            "from": from_expr,
            "to": to_expr,
        }
    )
    return f"{endpoints.pyroscope}/?{params}"


def profile_links(
    service_name: str,
    endpoints: Endpoints | None = None,
    *,
    from_expr: str = "now-1h",
    to_expr: str = "now",
) -> list[dict[str, str]]:
    endpoints = endpoints or Endpoints.from_env()
    selector = profile_selector(service_name)
    return [
        {
            "service": service_name,
            "selector": selector,
            "grafana_url": grafana_profile_url(service_name, endpoints, from_expr=from_expr, to_expr=to_expr),
            "pyroscope_url": pyroscope_profile_url(service_name, endpoints, from_expr=from_expr, to_expr=to_expr),
        }
    ]


def post_smoke_webhook(endpoints: Endpoints | None = None) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    run_id = os.getenv("WEBHOOKWISE_SMOKE_RUN_ID") or f"smoke-{int(time.time())}"
    payload = {
        "alertname": "WebhookWiseObservabilitySmoke",
        "source": "observability-smoke",
        "severity": "warning",
        "service": "webhookwise-api",
        "instance": "webhookwise-smoke",
        "run_id": run_id,
        "current_value": 1,
        "threshold": 1,
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "annotations": {
            "summary": "WebhookWise observability smoke synthetic alert",
            "description": "Generated by scripts/observability/webhookwise_observe.py smoke",
        },
    }
    body = json.dumps_bytes(payload)
    headers = {"X-Webhook-Source": "observability-smoke"}
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = signature
    return _post_json(f"{endpoints.api}/webhook/observability-smoke", payload, headers=headers)


def smoke(
    endpoints: Endpoints | None = None,
    *,
    send_webhook: bool = True,
    wait_seconds: int | None = None,
) -> list[dict[str, str]]:
    endpoints = endpoints or Endpoints.from_env()
    wait_seconds = wait_seconds if wait_seconds is not None else int(os.getenv("WEBHOOKWISE_SMOKE_WAIT_SECONDS", "8"))
    rows: list[dict[str, str]] = []

    health_rows = health(endpoints)
    ok_services = sum(1 for row in health_rows if row["status"] == "ok")
    rows.append(
        {
            "check": "health",
            "status": "ok" if ok_services == len(health_rows) else "error",
            "detail": f"{ok_services}/{len(health_rows)} endpoints ok",
        }
    )

    if send_webhook:
        try:
            result = post_smoke_webhook(endpoints)
            request_id = result.get("request_id") or result.get("id") or ""
            rows.append({"check": "webhook-post", "status": "ok", "detail": f"request_id={request_id}"})
        except RuntimeError as exc:
            rows.append({"check": "webhook-post", "status": "error", "detail": str(exc)[:200]})

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    prometheus_checks = {
        "prometheus-webhook-received": "sum(increase(webhook_received_total[10m]))",
        "prometheus-webhook-processed": "sum(increase(webhook_processed_total[10m]))",
        "prometheus-recording-rules": "max(webhook_events_active) or vector(0)",
        "prometheus-alerts": 'sum by (alertstate) (ALERTS{alertname=~"WebhookWise.*"}) or vector(0)',
    }
    for name, query in prometheus_checks.items():
        rows.append(_prometheus_smoke_row(name, query, endpoints))

    try:
        result = loki_query_range(
            '{service_name=~"webhookwise.*|webhookwise"} | json | trace_id != "-"',
            endpoints,
            limit=5,
            since_seconds=600,
        )
        streams = len(result.get("data", {}).get("result", []))
        rows.append({"check": "loki-trace-logs", "status": "ok" if streams else "warn", "detail": f"{streams} streams"})
    except RuntimeError as exc:
        rows.append({"check": "loki-trace-logs", "status": "error", "detail": str(exc)[:200]})

    try:
        result = tempo_search(endpoints)
        traces = len(result.get("traces") or result.get("data", {}).get("traces") or [])
        rows.append({"check": "tempo-search", "status": "ok" if traces else "warn", "detail": f"{traces} traces"})
    except RuntimeError as exc:
        rows.append({"check": "tempo-search", "status": "error", "detail": str(exc)[:200]})

    return rows


def runtime_acceptance(
    endpoints: Endpoints | None = None,
    *,
    send_webhook: bool = True,
    wait_seconds: int | None = None,
    dashboard_paths: tuple[str | Path, ...] = (
        "deploy/observability/grafana/dashboards/dashboard.json",
        "deploy/observability/grafana/dashboards/dashboard-diagnostics.json",
    ),
) -> list[dict[str, str]]:
    endpoints = endpoints or Endpoints.from_env()
    rows = smoke(endpoints, send_webhook=send_webhook, wait_seconds=wait_seconds)

    try:
        datasources = grafana_datasources(endpoints)
        uids = {str(item.get("uid", "")) for item in datasources}
        expected = {
            endpoints.prometheus_datasource_uid,
            endpoints.loki_datasource_uid,
            endpoints.tempo_datasource_uid,
            endpoints.pyroscope_datasource_uid,
        }
        missing = sorted(expected - uids)
        rows.append(
            {
                "check": "grafana-datasources",
                "status": "ok" if not missing else "error",
                "detail": "all expected datasources present" if not missing else f"missing={','.join(missing)}",
            }
        )
    except RuntimeError as exc:
        rows.append({"check": "grafana-datasources", "status": "error", "detail": str(exc)[:200]})

    for path in dashboard_paths:
        rows.append(_dashboard_acceptance_row(path, endpoints))

    slo_queries = {
        "slo-api-success": "webhookwise:http_request_success_ratio_5m",
        "slo-ingress-success": "webhookwise:webhook_ingress_success_ratio_5m",
        "slo-processing-success": "webhookwise:webhook_processing_success_ratio_5m",
        "slo-forward-success": "webhookwise:forward_delivery_success_ratio_5m",
        "slo-ai-degradation": "webhookwise:ai_degradation_ratio_5m",
        "slo-db-utilization": "webhookwise:db_pool_utilization_ratio",
        "slo-queue-backlog": "webhookwise:queue_backlog",
        "slo-redis-unavailable": "webhookwise:redis_unavailable_rate_5m",
    }
    for name, query in slo_queries.items():
        rows.append(_prometheus_smoke_row(name, query, endpoints))

    try:
        result = prometheus_query(
            'count({__name__=~".*_bucket", service_name=~"webhookwise.*"})',
            endpoints,
        )
        rows.append(
            {
                "check": "prometheus-histograms",
                "status": "ok" if result_rows(result) else "warn",
                "detail": f"{len(result_rows(result))} result rows",
            }
        )
    except RuntimeError as exc:
        rows.append({"check": "prometheus-histograms", "status": "error", "detail": str(exc)[:200]})

    return rows


def runbook_summary(
    alert_name: str,
    endpoints: Endpoints | None = None,
    *,
    since_seconds: int = 3600,
    limit: int = 5,
) -> list[dict[str, str]]:
    endpoints = endpoints or Endpoints.from_env()
    clean_alert = alert_name.strip()
    rows: list[dict[str, str]] = []

    alert_query = f'ALERTS{{alertname="{_promql_label_value(clean_alert)}"}}'
    rows.append(_prometheus_smoke_row("alert-state", alert_query, endpoints))

    for name, query in _runbook_promql_queries(clean_alert).items():
        rows.append(_prometheus_smoke_row(f"promql:{name}", query, endpoints))

    try:
        result = loki_query_range(
            '{service_name=~"webhookwise.*|webhookwise",severity=~"error|fatal|ERROR|FATAL"} | json',
            endpoints,
            limit=limit,
            since_seconds=since_seconds,
        )
        streams = len(result.get("data", {}).get("result", []))
        entries = sum(len(stream.get("values", [])) for stream in result.get("data", {}).get("result", []))
        rows.append(
            {
                "check": "loki:error-logs",
                "status": "ok" if entries else "warn",
                "detail": f"{entries} entries across {streams} streams",
            }
        )
    except RuntimeError as exc:
        rows.append({"check": "loki:error-logs", "status": "error", "detail": str(exc)[:200]})

    service_name = _service_for_alert(clean_alert)
    try:
        result = tempo_search(endpoints, service_name=service_name, limit=limit)
        traces = len(result.get("traces") or result.get("data", {}).get("traces") or [])
        rows.append(
            {
                "check": "tempo:recent-traces",
                "status": "ok" if traces else "warn",
                "detail": f"{traces} traces for {service_name}",
            }
        )
    except RuntimeError as exc:
        rows.append({"check": "tempo:recent-traces", "status": "error", "detail": str(exc)[:200]})

    rows.append(
        {
            "check": "profiles:links",
            "status": "ok",
            "detail": profile_links(service_name, endpoints)[0]["grafana_url"],
        }
    )
    return rows


def _prometheus_smoke_row(name: str, query: str, endpoints: Endpoints) -> dict[str, str]:
    try:
        result = prometheus_query(query, endpoints)
    except RuntimeError as exc:
        return {"check": name, "status": "error", "detail": str(exc)[:200]}
    series = len(result.get("data", {}).get("result", []))
    return {"check": name, "status": "ok" if series else "warn", "detail": f"{series} series"}


def _dashboard_acceptance_row(path: str | Path, endpoints: Endpoints) -> dict[str, str]:
    try:
        dashboard_rows = validate_dashboard_queries(path, endpoints)
    except RuntimeError as exc:
        return {"check": f"dashboard-promql:{Path(path).name}", "status": "error", "detail": str(exc)[:200]}
    errors = [row for row in dashboard_rows if row.get("status") != "success"]
    return {
        "check": f"dashboard-promql:{Path(path).name}",
        "status": "ok" if not errors else "error",
        "detail": f"{len(dashboard_rows) - len(errors)}/{len(dashboard_rows)} queries valid",
    }


def telemetry_contract(root: str | Path | None = None) -> list[dict[str, str]]:
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    rows: list[dict[str, str]] = []
    dashboard_paths = [
        root_path / "deploy/observability/grafana/dashboards/dashboard.json",
        root_path / "deploy/observability/grafana/dashboards/dashboard-diagnostics.json",
    ]
    rules_text = (root_path / "deploy/observability/prometheus/alerts.yml").read_text()
    metrics_text = (root_path / "core/observability/metrics.py").read_text()
    alloy_text = (root_path / "deploy/observability/alloy/config.alloy").read_text()
    env_text = (root_path / ".env.example.all").read_text()
    app_compose_text = (root_path / "deploy/compose/docker-compose.yml").read_text()
    docs_text = "\n".join(
        [
            (root_path / "docs/operations/observability/overview.md").read_text(),
            (root_path / "docs/operations/observability/dashboards.md").read_text(),
            (root_path / "docs/operations/observability/local-lab/README.md").read_text(),
        ]
    )

    defined_metrics = _defined_prometheus_metrics(metrics_text) | set(_RECORDING_RULE_RE.findall(rules_text))
    expressions = _dashboard_expressions(dashboard_paths) + _rule_expressions(rules_text)
    expanded_expressions = [expand_grafana_macros(expr) for expr in expressions]
    referenced_metrics = _referenced_prometheus_metrics(expanded_expressions)
    unknown = sorted(
        metric for metric in referenced_metrics if metric not in defined_metrics and not _is_external_metric(metric)
    )
    rows.append(_contract_row("dashboard-and-rules-metric-coverage", not unknown, _detail_list(unknown)))

    parse_errors = [expr for expr in expanded_expressions if not _promql_is_balanced(expr)]
    rows.append(_contract_row("promql-basic-parse", not parse_errors, f"{len(expressions)} expressions checked"))

    expressions_text = "\n".join(expressions)
    stale_names = sorted(
        name for name in _OLD_TELEMETRY_NAMES if re.search(rf"\b{re.escape(name)}\b", expressions_text)
    )
    rows.append(_contract_row("no-stale-telemetry-names", not stale_names, _detail_list(stale_names)))

    labels = _loki_label_names(alloy_text)
    high_cardinality = sorted(label for label in labels if label in _HIGH_CARDINALITY_LOKI_LABELS)
    rows.append(_contract_row("loki-label-cardinality", not high_cardinality, _detail_list(high_cardinality)))

    schema_checks = {
        "env": "OTEL_SCHEMA_URL=https://opentelemetry.io/schemas/1.41.0" in env_text,
        "compose": "OTEL_SCHEMA_URL: ${OTEL_SCHEMA_URL:-https://opentelemetry.io/schemas/1.41.0}" in app_compose_text,
        "docs": "https://opentelemetry.io/schemas/1.41.0" in docs_text,
    }
    rows.append(
        _contract_row(
            "otel-schema-version-consistency",
            all(schema_checks.values()),
            ",".join(name for name, ok in schema_checks.items() if not ok) or "schema 1.41.0 consistent",
        )
    )

    direct_extra = _find_direct_logging_extra(root_path)
    rows.append(_contract_row("structured-log-helper-required", not direct_extra, _detail_list(direct_extra)))

    sensitive_label_hits = sorted(label for label in labels if _looks_sensitive_label(label))
    rows.append(_contract_row("no-sensitive-loki-labels", not sensitive_label_hits, _detail_list(sensitive_label_hits)))

    return rows


def _contract_row(check: str, ok: bool, detail: str) -> dict[str, str]:
    return {"check": check, "status": "ok" if ok else "error", "detail": detail or "ok"}


def _detail_list(items: list[str]) -> str:
    if not items:
        return "ok"
    preview = ", ".join(items[:10])
    return preview if len(items) <= 10 else f"{preview}, ... (+{len(items) - 10})"


def _defined_prometheus_metrics(metrics_text: str) -> set[str]:
    names: set[str] = set()
    for kind, dotted_name, unit in _metric_definitions(metrics_text):
        bases = _prometheus_metric_bases(dotted_name, unit)
        if kind == "Counter":
            for base in bases:
                names.update({base, f"{base}_total"})
        elif kind == "Histogram":
            for base in bases:
                names.update({f"{base}_bucket", f"{base}_count", f"{base}_sum"})
        elif kind == "Gauge":
            for base in bases:
                names.update({base, f"{base}_ratio"})
    return names


def _metric_definitions(metrics_text: str) -> list[tuple[str, str, str]]:
    definitions: list[tuple[str, str, str]] = []
    try:
        tree = ast.parse(metrics_text)
    except SyntaxError:
        for kind, dotted_name in _OTEL_METRIC_RE.findall(metrics_text):
            definitions.append((kind, dotted_name, "1"))
        return definitions

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        kind = node.func.id
        if kind not in {"Counter", "Gauge", "Histogram"} or not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            continue
        unit = "1"
        for keyword in node.keywords:
            if (
                keyword.arg == "unit"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                unit = keyword.value.value
        definitions.append((kind, first_arg.value, unit))
    return definitions


def _prometheus_metric_bases(dotted_name: str, unit: str) -> set[str]:
    base = dotted_name.replace(".", "_")
    if unit == "s":
        return {base, f"{base}_seconds"}
    if unit == "By":
        return {base, f"{base}_bytes"}
    if unit and unit != "1":
        return {base, f"{base}_{unit}"}
    return {base}


def _dashboard_expressions(paths: list[Path]) -> list[str]:
    expressions: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        expressions.extend(query["expr"] for query in dashboard_queries(path))
    return expressions


def _rule_expressions(rules_text: str) -> list[str]:
    expressions: list[str] = []
    block: list[str] = []
    pending = False

    def flush_block() -> None:
        nonlocal block, pending
        if block:
            expressions.append("\n".join(block))
        block = []
        pending = False

    for raw_line in rules_text.splitlines():
        line = raw_line.strip()
        if line.startswith("expr:"):
            flush_block()
            value = line.removeprefix("expr:").strip()
            if value and value != "|":
                expressions.append(value)
            else:
                pending = True
            continue
        if pending:
            if line.startswith(("- ", "labels:", "annotations:")):
                flush_block()
            elif line:
                block.append(line)
    flush_block()
    return expressions


def _referenced_prometheus_metrics(expressions: list[str]) -> set[str]:
    metrics: set[str] = set()
    for expr in expressions:
        without_strings = _QUOTED_RE.sub('""', expr)
        without_label_matchers = re.sub(r"\{[^{}]*\}", "{}", without_strings)
        without_grouping = re.sub(r"\b(?:by|without)\s*\([^)]*\)", "", without_label_matchers)
        metrics.update(_PROMQL_METRIC_TOKEN_RE.findall(without_grouping))
    return {metric for metric in metrics if metric not in _PROMQL_FUNCTIONS}


def _is_external_metric(metric: str) -> bool:
    return metric in _EXTERNAL_METRICS or metric.startswith(_EXTERNAL_METRIC_PREFIXES)


def _promql_is_balanced(expr: str) -> bool:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in _QUOTED_RE.sub('""', expr):
        if char in "([{":
            stack.append(char)
        elif char in pairs and (not stack or stack.pop() != pairs[char]):
            return False
    return not stack


def _loki_label_names(alloy_text: str) -> set[str]:
    labels: set[str] = set()
    for block in re.findall(r"action\s*\{(.*?)\}", alloy_text, flags=re.DOTALL):
        if "loki.resource.labels" not in block and "loki.attribute.labels" not in block:
            continue
        match = re.search(r'value\s*=\s*"([^"]+)"', block)
        if match:
            labels.update(label.strip() for label in match.group(1).split(",") if label.strip())
    return labels


def _find_direct_logging_extra(root: Path) -> list[str]:
    hits: list[str] = []
    for base in ("core", "api", "services", "db"):
        for path in (root / base).rglob("*.py"):
            if path.name == "log_attrs.py":
                continue
            text = path.read_text()
            if "extra={" in text:
                hits.append(str(path.relative_to(root)))
    return hits


def _looks_sensitive_label(label: str) -> bool:
    lowered = label.lower()
    return any(word in lowered for word in ("token", "secret", "password", "authorization", "cookie", "prompt"))


def _promql_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _service_for_alert(alert_name: str) -> str:
    lowered = alert_name.lower()
    if "worker" in lowered or "processing" in lowered or "forward" in lowered or "queue" in lowered:
        return "webhookwise-worker"
    if "scheduler" in lowered:
        return "webhookwise-scheduler"
    return "webhookwise-api"


def _runbook_promql_queries(alert_name: str) -> dict[str, str]:
    lowered = alert_name.lower()
    common = {
        "api-5xx-rate": PROMQL_PRESETS["api-5xx-rate"],
        "api-latency-p95": PROMQL_PRESETS["api-latency-p95"],
    }
    if "ingress" in lowered:
        return {
            "ingress-success": "webhookwise:webhook_ingress_success_ratio_5m",
            "payload-p95": PROMQL_PRESETS["webhook-payload-p95"],
            **common,
        }
    if "processing" in lowered or "deadletter" in lowered or "deadletters" in lowered:
        return {
            "processing-success": "webhookwise:webhook_processing_success_ratio_5m",
            "processing-latency-p95": "webhookwise:webhook_processing_duration_p95_5m",
            "pipeline-step-latency-p95": PROMQL_PRESETS["pipeline-step-latency-p95"],
            "worker-runs": PROMQL_PRESETS["worker-runs"],
        }
    if "forward" in lowered or "circuitbreaker" in lowered or "circuit" in lowered:
        return {
            "forward-success": "webhookwise:forward_delivery_success_ratio_5m",
            "forward-latency-p95": "webhookwise:forward_delivery_duration_p95_5m",
            "outbox-backlog-age": PROMQL_PRESETS["forward-outbox-backlog-age"],
            "circuit-breaker-state": PROMQL_PRESETS["circuit-breaker-state"],
        }
    if "ai" in lowered:
        return {
            "ai-degradation": "webhookwise:ai_degradation_ratio_5m",
            "ai-latency-p95": PROMQL_PRESETS["ai-latency-p95"],
            "ai-cache-rate": PROMQL_PRESETS["ai-cache-rate"],
            "ai-cost": PROMQL_PRESETS["ai-cost"],
        }
    if "db" in lowered:
        return {
            "db-health": 'max(db_health_state{db_state="unhealthy"}) or vector(0)',
            "db-pool-utilization": "webhookwise:db_pool_utilization_ratio",
            "db-latency-p95": PROMQL_PRESETS["db-latency-p95"],
        }
    if "redis" in lowered:
        return {
            "redis-unavailable": "webhookwise:redis_unavailable_rate_5m",
            "redis-latency-p95": PROMQL_PRESETS["redis-latency-p95"],
            "queue-ops": PROMQL_PRESETS["queue-ops"],
        }
    if "queue" in lowered:
        return {
            "queue-backlog": "webhookwise:queue_backlog",
            "queue-ops": PROMQL_PRESETS["queue-ops"],
            "worker-latency-p95": PROMQL_PRESETS["worker-latency-p95"],
        }
    if "loki" in lowered or "collector" in lowered or "alloy" in lowered:
        return {
            "collector-health": PROMQL_PRESETS["collector-health"],
            "collector-queue": PROMQL_PRESETS["collector-queue"],
            "loki-write-retries": PROMQL_PRESETS["loki-write-retries"],
        }
    return {
        "api-success": "webhookwise:http_request_success_ratio_5m",
        **common,
    }


def grafana_dashboard(uid: str = "webhook-wise-aiops", endpoints: Endpoints | None = None) -> dict[str, Any]:
    endpoints = endpoints or Endpoints.from_env()
    return _request_json(
        f"{endpoints.grafana}/api/dashboards/uid/{uid}",
        **_grafana_auth(endpoints),
    )


def grafana_datasources(endpoints: Endpoints | None = None) -> list[dict[str, Any]]:
    endpoints = endpoints or Endpoints.from_env()
    result = _request_json(f"{endpoints.grafana}/api/datasources", **_grafana_auth(endpoints))
    if not isinstance(result, list):
        raise RuntimeError("Grafana returned an unexpected datasource response")
    return result


def health(endpoints: Endpoints | None = None) -> list[dict[str, str]]:
    endpoints = endpoints or Endpoints.from_env()
    if endpoints.query_mode == "grafana-proxy":
        return [
            _health_row("grafana", f"{endpoints.grafana}/api/health", auth=_grafana_auth(endpoints)),
            _health_prometheus_proxy(endpoints),
            _health_loki_proxy(endpoints),
        ]
    checks = [
        ("api", f"{endpoints.api}/ready"),
        ("prometheus", f"{endpoints.prometheus}/-/ready"),
        ("loki", f"{endpoints.loki}/ready"),
        ("tempo", f"{endpoints.tempo}/ready"),
        ("grafana", f"{endpoints.grafana}/api/health"),
        ("pyroscope", endpoints.pyroscope),
        ("alloy", f"{endpoints.alloy}/-/ready"),
    ]
    return [_health_row(name, url) for name, url in checks]


def _health_row(name: str, url: str, *, auth: dict[str, Any] | None = None) -> dict[str, str]:
    try:
        text = _request_text(url, timeout=5, **(auth or {}))
        return {"service": name, "status": "ok", "detail": text.strip()[:120]}
    except RuntimeError as exc:
        return {"service": name, "status": "error", "detail": str(exc)[:200]}


def _grafana_auth(endpoints: Endpoints) -> dict[str, Any]:
    if endpoints.grafana_token:
        return {"bearer_token": endpoints.grafana_token}
    return {"basic_auth": (endpoints.grafana_user, endpoints.grafana_password)}


def _health_prometheus_proxy(endpoints: Endpoints) -> dict[str, str]:
    try:
        result = prometheus_query("up", endpoints)
        count = len(result.get("data", {}).get("result", []))
        return {"service": "prometheus-proxy", "status": "ok", "detail": f"{count} up series via Grafana"}
    except RuntimeError as exc:
        return {"service": "prometheus-proxy", "status": "error", "detail": str(exc)[:200]}


def _health_loki_proxy(endpoints: Endpoints) -> dict[str, str]:
    try:
        result = _request_json(
            f"{endpoints.grafana}/api/datasources/proxy/uid/{endpoints.loki_datasource_uid}/loki/api/v1/labels",
            **_grafana_auth(endpoints),
        )
        count = len(result.get("data", []))
        return {"service": "loki-proxy", "status": "ok", "detail": f"{count} labels via Grafana"}
    except RuntimeError as exc:
        return {"service": "loki-proxy", "status": "error", "detail": str(exc)[:200]}


def dashboard_queries(path: str | Path = "deploy/observability/grafana/dashboards/dashboard.json") -> list[dict[str, str]]:
    raw = json.loads(Path(path).read_text())
    queries: list[dict[str, str]] = []
    for panel in raw.get("panels", []):
        for target in panel.get("targets", []) or []:
            expr = target.get("expr")
            if expr:
                queries.append(
                    {
                        "panel": str(panel.get("title", "")),
                        "refId": str(target.get("refId", "")),
                        "expr": str(expr),
                    }
                )
    return queries


def expand_grafana_macros(expr: str, *, rate_interval: str = "5m", dashboard_range: str = "6h") -> str:
    return expr.replace("$__rate_interval", rate_interval).replace("$__range", dashboard_range)


def validate_dashboard_queries(
    path: str | Path = "deploy/observability/grafana/dashboards/dashboard.json",
    endpoints: Endpoints | None = None,
) -> list[dict[str, Any]]:
    endpoints = endpoints or Endpoints.from_env()
    rows: list[dict[str, Any]] = []
    for query in dashboard_queries(path):
        expr = expand_grafana_macros(query["expr"])
        try:
            result = prometheus_query(expr, endpoints)
            rows.append(
                {
                    "panel": query["panel"],
                    "refId": query["refId"],
                    "status": result.get("status", "unknown"),
                    "series": len(result.get("data", {}).get("result", [])),
                    "expr": query["expr"],
                }
            )
        except RuntimeError as exc:
            rows.append(
                {
                    "panel": query["panel"],
                    "refId": query["refId"],
                    "status": "error",
                    "series": 0,
                    "error": str(exc),
                    "expr": query["expr"],
                }
            )
    return rows


def result_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in result.get("data", {}).get("result", []):
        metric = item.get("metric", {})
        value = item.get("value", [None, None])
        rows.append({"metric": metric, "timestamp": value[0], "value": value[1]})
    return rows
