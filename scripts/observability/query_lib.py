"""Shared helpers for querying the local WebhookWise observability stack.

The module intentionally uses only the Python standard library so it can run in
the app container, on a developer laptop, or from a lightweight MCP wrapper.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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
        )


PROMQL_PRESETS: dict[str, str] = {
    "api-rate": 'sum by (http_route, http_status_code) (rate(http_server_requests_total{service_name="webhookwise-api"}[5m]))',
    "api-latency-p95": (
        "histogram_quantile(0.95, sum by (le, http_route) "
        '(rate(http_server_request_duration_seconds_bucket{service_name="webhookwise-api"}[5m])))'
    ),
    "api-5xx-rate": (
        '100 * ((sum(rate(http_server_requests_total{service_name="webhookwise-api", http_status_code=~"5.."}[5m])) '
        'or vector(0)) / clamp_min((sum(rate(http_server_requests_total{service_name="webhookwise-api"}[5m])) '
        "or vector(0)), 0.000001))"
    ),
    "webhook-rate": "sum by (webhook_source) (rate(webhook_received_total[5m]))",
    "active-events": "max(webhook_events_count_ratio) or vector(0)",
    "queue-backlog": "max(queue_depth_ratio) or vector(0) or max(queue_pending_ratio) or max(queue_lag_ratio)",
    "queue-ops": "sum by (queue_operation, queue_status) (rate(queue_operations_total[5m]))",
    "worker-runs": "sum by (worker_task_name, worker_task_status) (rate(worker_task_runs_total[5m]))",
    "worker-latency-p95": (
        "histogram_quantile(0.95, sum by (le, worker_task_name) " "(rate(worker_task_duration_seconds_bucket[5m])))"
    ),
    "db-pool": "max(db_pool_connections_checked_out_ratio) or vector(0) or max(db_pool_connections_max_ratio)",
    "db-latency-p95": (
        "histogram_quantile(0.95, sum by (le, db_operation) " "(rate(db_session_duration_seconds_bucket[5m])))"
    ),
    "redis-latency-p95": (
        "histogram_quantile(0.95, sum by (le, redis_operation) " "(rate(redis_operation_duration_seconds_bucket[5m])))"
    ),
    "scheduler-lag": "max by (scheduler_task_name) (scheduler_task_lag_seconds) or vector(0)",
    "scheduler-last-success-age": "time() - max by (scheduler_task_name) (scheduler_task_last_success_unixtime_seconds)",
    "noise-rate": "sum by (webhook_relation, webhook_suppressed) (rate(webhook_suppressed_total[5m]))",
    "suppression-rate": (
        '100 * ((sum(rate(webhook_suppressed_total{webhook_suppressed="true"}[5m])) or vector(0)) / '
        "clamp_min((sum(rate(webhook_suppressed_total[5m])) or vector(0)), 0.000001))"
    ),
    "ai-latency-p95": (
        "histogram_quantile(0.95, sum by (le, ai_engine) " "(rate(ai_request_duration_seconds_bucket[5m])))"
    ),
    "ai-cost": "sum(ai_cost_USD_total) or vector(0)",
    "ai-tokens": "sum by (ai_model, ai_token_type) (increase(ai_tokens_total[6h])) or vector(0)",
    "ai-cache-rate": (
        "sum by (ai_cache_operation, ai_cache_result) " "(rate(ai_cache_requests_total[5m])) or vector(0)"
    ),
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
    "webhook-status": "max by (webhook_status) (webhook_processing_status_count_ratio) or vector(0)",
    "webhook-stuck": "max by (webhook_status) (webhook_stuck_status_count_ratio) or vector(0)",
    "pipeline-step-latency-p95": (
        "histogram_quantile(0.95, sum by (le, pipeline_step) "
        "(rate(webhook_pipeline_step_duration_seconds_bucket[5m])))"
    ),
    "queue-operation-latency-p95": (
        "histogram_quantile(0.95, sum by (le, queue_operation) " "(rate(queue_operation_duration_seconds_bucket[5m])))"
    ),
    "webhook-payload-p95": (
        "histogram_quantile(0.95, sum by (le, webhook_source) " "(rate(webhook_ingress_payload_size_bytes_bucket[5m])))"
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
        "sum(rate(faro_receiver_events_total[5m])) or vector(0) or " "sum(rate(faro_receiver_measurements_total[5m]))"
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
        'count by (deployment_environment, service_name, job) ({__name__=~"http_server_requests_total|'
        'worker_task_runs_total|scheduler_task_runs_total|ai_tokens_total|process_cpu_utilization_ratio"})'
    ),
    "process-memory": "sum by (service_name) (process_memory_usage_bytes) or vector(0)",
    "service-graph-rate": (
        "sum by (client, server, connection_type) " "(rate(traces_service_graph_request_total[5m])) or vector(0)"
    ),
    "service-graph-failures": (
        "sum by (client, server, connection_type) " "(rate(traces_service_graph_request_failed_total[5m])) or vector(0)"
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
        return json.loads(body)
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
            return response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url} failed: {exc}") from exc


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


def dashboard_queries(path: str | Path = "grafana/dashboard.json") -> list[dict[str, str]]:
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
    path: str | Path = "grafana/dashboard.json",
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
