# Grafana Dashboard Guide

WebhookWise ships two provisioned Grafana dashboards:

- `grafana/dashboard.json`: 基础大盘，面向日常值班和 SLO 观察。
- `grafana/dashboard-diagnostics.json`: 深度诊断大盘，面向链路、RUM、Beyla、k6、payload、AI cache、outbox 等细节排查。

The dashboards are intentionally aligned to the OpenTelemetry metric names emitted by
`core.observability.metrics` and transformed by Alloy into Prometheus series.

Local entry:

```text
http://localhost:3000/d/webhook-wise-aiops/webhookwise-aiops-e5a4a7-e79b98
```

Provisioning path:

```text
grafana/*.json -> docker compose volume -> /var/lib/grafana/dashboards
deploy/observability/grafana-dashboards.yml -> file provider
```

If a local Grafana page does not pick up file changes, restart only Grafana:

```bash
docker compose -p webhookwise --env-file .env -f deploy/compose/docker-compose.infra.yml -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.observability.yml restart grafana
```

Whenever panels are added or their grouping labels change, regenerate the
Logs / Trace / Profile drill-down links:

```bash
python scripts/observability/update_dashboard_links.py
```

Those links inherit the current Grafana time range and add field-label context
when available, such as `webhook_source`, `service_name`, `worker_task_name`,
`pipeline_step`, or `forward_target_type`.

## Dashboard Coverage

| Row | Panels | Primary question |
| --- | --- | --- |
| 系统入口与 HTTP | Webhook QPS, active DB records, API request rate, HTTP status distribution, API latency, API 5xx rate, security checks | Is traffic entering the API, and is the HTTP layer healthy? |
| 队列、Worker 与 Pipeline | Queue pending/lag, retained stream length, queue operation rate, worker runs, worker duration, webhook processing duration, pipeline step rate, running tasks, dead letters, semaphore timeouts, storm suppression | Did the webhook enter the async pipeline, and can workers keep up? |
| 数据库与 Redis | DB pool usage, DB session rate/latency, Redis operation rate/latency | Are persistence or broker calls slow or failing? |
| Scheduler 与后台任务 | Scheduler runs, duration, lag, time since last success | Are periodic poll/outbox/maintenance jobs running on time? |
| AIOps、AI 与转发 | Noise reduction, suppression rate, AI cost, AI latency, forward delivery, forward latency, circuit breaker state, outbox oldest backlog age, events/signals | Are AIOps decisions, AI calls, and delivery outcomes healthy? |
| SLO、告警与链路闭环 | API availability, webhook completion, AI degradation, forward success, Prometheus alert state, event/trace correlation | Are user-facing SLOs healthy, and can I jump from alert to logs/traces quickly? |

Deep diagnostic dashboard rows:

| Row | Panels | Primary question |
| --- | --- | --- |
| 可观测后端、RUM、Beyla 与压测 | Faro receiver, Beyla span metrics, process CPU, k6 smoke results, Alloy/Loki write health | Are telemetry collection, frontend RUM, eBPF, and synthetic checks working? |
| 环境与容量 | Current environment/service inventory, process memory, active HTTP requests and request-body P95 | Am I looking at the expected environment, and are service resources normal? |
| Webhook 与 Pipeline 深度诊断 | Processing status, pipeline step P95, queue operation P95, payload P95, status-transition rate | Where is webhook processing slow? |
| AI、降噪与转发补充 | AI tokens, AI cache, deep analysis, noise evaluation rate/latency, outbox lifecycle/latency, cost by model | What drives AI cost and delivery behavior? |
| 采集链路与服务拓扑补充 | Service graph calls/failures, process IO, OTel exporter queue, Loki write latency/retries, Faro limiter | Is the telemetry pipeline itself healthy? |

## No Data Rules

`No data` has three common meanings:

| Case | Meaning | Action |
| --- | --- | --- |
| Old metric name | The panel uses a metric that no longer exists after OTel naming changes | Update PromQL to the current metric catalog |
| Cold business path | The metric only exists after a path runs, such as AI, forwarding, scheduler, or Faro | Trigger the path or widen the time range |
| Histogram with no recent samples | The bucket series exists but has no rate in the selected interval, so quantile can be empty or `NaN` | Run traffic or increase the time range/rate interval |

Stat panels that should represent absence as zero use `or vector(0)`. Latency
histograms do not always use zero fallback because a zero latency can be
misleading; no recent samples should be read as "that path did not run".

## Current Metric Naming

Use Prometheus names, not the Python OTel instrument names, in dashboard panels.

| Domain | Prometheus metric examples | Important labels |
| --- | --- | --- |
| HTTP/API | `http_server_request_duration_seconds_count`, `http_server_request_duration_seconds_bucket` | `service_name`, `http_route`, `http_response_status_code`, `http_request_method` |
| Webhook ingress | `webhook_received_total`, `webhook_ingress_payload_size_bytes_bucket` | `webhook_source`, `webhook_status`, `webhook_outcome` |
| Queue | `queue_operations_total`, `queue_pending`, `queue_lag`, `queue_depth` | `queue_name`, `queue_operation`, `queue_status`, `queue_stream`, `queue_group`; `queue_depth` is Redis Stream retained length, not unconsumed backlog |
| Worker/pipeline | `worker_task_runs_total`, `worker_task_duration_seconds_bucket`, `webhook_pipeline_steps_total`, `webhook_processing_duration_seconds_bucket` | `worker_task_name`, `worker_task_status`, `pipeline_step`, `webhook_outcome` |
| DB/Redis | `db_sessions_total`, `db_session_duration_seconds_bucket`, `redis_operations_total`, `redis_operation_duration_seconds_bucket` | `db_operation`, `db_status`, `redis_operation`, `redis_status` |
| Scheduler | `scheduler_task_runs_total`, `scheduler_task_duration_seconds_bucket`, `scheduler_task_lag_seconds`, `scheduler_task_last_success_unixtime_seconds` | `scheduler_task_name`, `scheduler_task_status` |
| AIOps/AI | `webhook_noise_evaluations_total`, `webhook_noise_evaluation_duration_seconds_bucket`, `ai_request_duration_seconds_bucket`, `ai_tokens_total`, `ai_cost_USD_total`, `ai_cache_requests_total`, `ai_deep_analysis_total` | `webhook_relation`, `webhook_suppressed`, `ai_engine`, `ai_model`, `ai_token_type`, `ai_cache_result` |
| Forwarding | `forward_delivery_total`, `forward_delivery_duration_seconds_bucket`, `forward_outbox_records_total` | `forward_target_type`, `forward_status` |
| Events/signals | `observability_events_total`, `observability_signals_total` | OTel attributes `event.name`, `signal.name`, `signal.state` exposed as Prometheus-safe labels `event_name`, `signal_name`, `signal_state` |
| Faro | `faro_receiver_events_total`, `faro_receiver_measurements_total`, `faro_receiver_exceptions_total`, `faro_receiver_logs_total` | Alloy component labels |
| Beyla / service graph | `traces_span_metrics_calls_total`, `traces_span_metrics_duration_seconds_bucket`, `traces_service_graph_request_total`, `traces_service_graph_request_failed_total`, `process_cpu_utilization_ratio`, `process_memory_usage_bytes` | `source`, `service_name`, `span_name`, `span_kind`, `client`, `server`, `connection_type` |
| k6 | `k6_http_reqs_total`, `k6_http_req_failed_rate`, `k6_http_req_duration_p95`, `k6_checks_rate` | k6 remote write labels |
| Collection layer | `alloy_config_last_load_successful`, `alloy_component_controller_running_components`, `loki_write_dropped_entries_total` | Alloy component labels |

Some OpenTelemetry observable gauges are exposed by the Prometheus exporter with
a `_ratio` suffix even when they are counts. The local rule file records friendly
aliases such as `queue_pending`, `queue_lag`, `queue_depth`,
`webhook_events_active`, `webhook_processing_status_count`, and
`db_pool_connections_checked_out`. Dashboard queries use those recording rule
names directly, so Grafana panels match the project metric vocabulary.

SLO / RED / USE panels should prefer the recording rules in
`deploy/observability/alerts.yml`:

| Layer | Recording rules |
| --- | --- |
| API RED | `webhookwise:http_request_rate_5m`, `webhookwise:http_request_success_ratio_5m`, `webhookwise:http_request_duration_p95_5m` |
| Webhook SLO | `webhookwise:webhook_ingress_success_ratio_5m`, `webhookwise:webhook_processing_success_ratio_5m`, `webhookwise:webhook_processing_duration_p95_5m` |
| Forwarding / AI | `webhookwise:forward_delivery_success_ratio_5m`, `webhookwise:forward_delivery_duration_p95_5m`, `webhookwise:ai_degradation_ratio_5m` |
| USE / dependencies | `webhookwise:db_pool_utilization_ratio`, `webhookwise:queue_backlog`, `webhookwise:redis_unavailable_rate_5m` |

Prometheus and Alloy are configured to retain exemplars, and the Prometheus
datasource points exemplar `trace_id` values at Tempo.

## Quick PromQL Sanity Checks

Use these when a panel looks suspicious:

```promql
count by (__name__) ({
  __name__=~"http_server_request_duration_seconds_count|webhook_received_total|webhook_noise_evaluations_total|ai_request_duration_seconds_bucket"
})
```

```promql
sum by (http_route, http_response_status_code) (
  rate(http_server_request_duration_seconds_count{service_name="webhookwise-api"}[5m])
)
```

```promql
sum by (webhook_relation, webhook_suppressed) (
  rate(webhook_noise_evaluations_total{webhook_suppressed="true"}[5m])
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le, ai_engine) (
    rate(ai_request_duration_seconds_bucket[5m])
  )
)
```

```promql
sum by (ai_model, ai_token_type) (
  increase(ai_tokens_total[6h])
)
```

```promql
histogram_quantile(
  0.95,
  sum by (le, pipeline_step) (
    rate(webhook_pipeline_step_duration_seconds_bucket[5m])
  )
)
```

## Change Checklist

When adding or renaming an observability metric:

1. Update `docs/operations/observability/overview.md` component catalog.
2. Update `docs/operations/observability/local-lab/metrics.md` query examples and explanations.
3. Update `grafana/dashboard.json` if the metric should be monitored on the dashboard.
4. Validate the PromQL against local Prometheus.
5. Decide whether absence should mean `0` or real `No data`.
