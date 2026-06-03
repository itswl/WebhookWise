---
name: webhookwise-observability
description: Query WebhookWise project data from the local observability stack. Use for WebhookWise metrics, logs, Grafana dashboard checks, Prometheus PromQL, Loki LogQL, Faro RUM, Beyla, k6, Pyroscope-related triage, and Chinese requests like "查项目数据", "看可观测", "为什么 No data", "查日志/指标/大盘".
---

# WebhookWise Observability

Use this skill when the user asks to inspect WebhookWise runtime data, observability signals, Grafana panels, Prometheus metrics, Loki logs, Faro RUM, Beyla eBPF data, k6 results, or local stack health.

## Tooling

Prefer the repository helper CLI:

```bash
python scripts/observability/webhookwise_observe.py health
python scripts/observability/webhookwise_observe.py preset --list
python scripts/observability/webhookwise_observe.py preset api-rate
python scripts/observability/webhookwise_observe.py promql 'sum by (http_response_status_code) (increase(http_server_request_duration_seconds_count[1h]))'
python scripts/observability/webhookwise_observe.py logs --query '{service_name="webhookwise"} | json' --limit 20
python scripts/observability/webhookwise_observe.py tempo --service-name webhookwise-api --limit 5
python scripts/observability/webhookwise_observe.py profiles --service-name webhookwise-api
python scripts/observability/webhookwise_observe.py dashboard --validate
python scripts/observability/webhookwise_observe.py smoke
python scripts/observability/webhookwise_observe.py contract
python scripts/observability/webhookwise_observe.py acceptance
python scripts/observability/webhookwise_observe.py runbook WebhookWiseApiAvailabilityFastBurn
```

For MCP-style stdio clients, start:

```bash
python scripts/observability/webhookwise_mcp.py
```

It exposes tools:

- `webhookwise_health`
- `webhookwise_datasources`
- `webhookwise_promql`
- `webhookwise_preset`
- `webhookwise_logs`
- `webhookwise_tempo_search`
- `webhookwise_profiles`
- `webhookwise_dashboard_validate`
- `webhookwise_smoke`
- `webhookwise_acceptance`
- `webhookwise_contract`
- `webhookwise_runbook`

## Endpoints

Defaults are local compose URLs:

- Prometheus: `http://localhost:9090`
- Loki: `http://localhost:3100`
- Tempo: `http://localhost:3200`
- Grafana: `http://localhost:3000`
- Pyroscope: `http://localhost:4040`
- Alloy: `http://localhost:12345`
- API: `http://localhost:8000`

Override with environment variables:

- `WEBHOOKWISE_PROMETHEUS_URL`
- `WEBHOOKWISE_LOKI_URL`
- `WEBHOOKWISE_TEMPO_URL`
- `WEBHOOKWISE_GRAFANA_URL`
- `WEBHOOKWISE_PYROSCOPE_URL`
- `WEBHOOKWISE_ALLOY_URL`
- `WEBHOOKWISE_API_URL`
- `WEBHOOKWISE_GRAFANA_USER`
- `WEBHOOKWISE_GRAFANA_PASSWORD`

## Online Query Mode

When the user asks how to query online or production data, use Grafana datasource
proxy mode. This keeps Prometheus and Loki private and sends queries through the
authenticated Grafana API.

```bash
export WEBHOOKWISE_QUERY_MODE=grafana-proxy
export WEBHOOKWISE_GRAFANA_URL=https://webhook-grafana.wetalk.eu.org
export WEBHOOKWISE_GRAFANA_TOKEN='<grafana-service-account-token>'
python scripts/observability/webhookwise_observe.py datasources
python scripts/observability/webhookwise_observe.py health
python scripts/observability/webhookwise_observe.py preset api-rate
python scripts/observability/webhookwise_observe.py logs --query '{service_name="webhookwise-api"} | json' --limit 20
```

If the datasource UIDs are not `prometheus` and `loki`, set:

```bash
export WEBHOOKWISE_PROMETHEUS_DATASOURCE_UID='<prometheus-datasource-uid>'
export WEBHOOKWISE_LOKI_DATASOURCE_UID='<loki-datasource-uid>'
export WEBHOOKWISE_TEMPO_DATASOURCE_UID='<tempo-datasource-uid>'
export WEBHOOKWISE_PYROSCOPE_DATASOURCE_UID='<pyroscope-datasource-uid>'
```

The helper sends `WebhookWise-Observability/0.1` as its default `User-Agent`.
Override it with `WEBHOOKWISE_HTTP_USER_AGENT` only when a front door requires a
different client identity.

Never commit Grafana tokens or passwords.

## Workflow

1. Start with `health` if the user asks why data is missing or whether the stack is alive.
2. Use `dashboard --validate` when Grafana panels show `No data` or after editing `deploy/observability/grafana/dashboards/dashboard.json`.
3. Use `preset --list` then `preset <name>` for common metrics. Prefer presets over rewriting PromQL.
4. Use `promql` for custom metric questions.
5. Use `logs` for concrete events, errors, trace IDs, or frontend Faro records.
6. Use `tempo --service-name <service>` when the user asks whether trace data exists or needs trace examples.
7. Use `profiles --service-name <service>` when CPU, latency, or worker backlog needs Pyroscope investigation.
8. Use `runbook <alert_name>` when an alert is firing or the user asks for incident context.
9. Explain whether absence means zero traffic, cold business path, stale k6 data, or a wrong metric name.
10. Use `contract` after telemetry edits and `smoke`/`acceptance` after runtime config changes.

## Useful Presets

- API: `api-rate`, `api-latency-p95`, `api-5xx-rate`
- Webhook: `webhook-rate`, `active-events`, `noise-rate`, `suppression-rate`
- Queue/worker: `queue-backlog`, `queue-retained-depth`, `queue-ops`, `worker-runs`, `worker-latency-p95`
- DB/Redis: `db-pool`, `db-latency-p95`, `redis-latency-p95`
- Scheduler: `scheduler-lag`, `scheduler-last-success-age`
- SLO: `slo-api-success`, `slo-ingress-success`, `slo-processing-success`, `slo-forward-success`, `slo-ai-degradation`, `slo-db-utilization`, `slo-queue-backlog`
- AI/forwarding: `ai-latency-p95`, `ai-cost`, `ai-tokens`, `ai-cache-rate`, `deep-analysis-rate`, `forward-rate`, `forward-outbox-rate`, `forward-outbox-backlog-age`, `circuit-breaker-state`
- Deep diagnostics: `webhook-status`, `webhook-stuck`, `pipeline-step-latency-p95`, `queue-operation-latency-p95`, `webhook-payload-p95`, `noise-evaluations`
- Frontend/eBPF/load/collector: `faro-rum`, `beyla-calls`, `k6-smoke`, `collector-health`, `environment-services`, `process-memory`, `service-graph-rate`, `service-graph-failures`, `loki-write-latency-p95`, `loki-write-retries`

## Interpretation Notes

- `No data` often means the business path did not run in the selected range. Latency histograms should not be forced to zero.
- Stat/count panels may use `or vector(0)` when absence should mean no events.
- k6 writes stale markers after a run; use range functions such as `max_over_time(k6_http_req_duration_p95[6h])`.
- Current metric labels use OTel-to-Prometheus names such as `webhook_source`, `http_response_status_code`, `ai_engine`, `webhook_relation`, and `webhook_suppressed`.
- Friendly recording rules hide confusing OTel gauge suffixes where possible, for example `queue_pending` falls back to `queue_pending_ratio` and `webhook_events_active` falls back to `webhook_events_count_ratio`.
- Trace/log correlation is bidirectional: Tempo `tracesToLogsV2` maps `service.name` to Loki `service_name`, and Loki derived fields link JSON `trace_id` back to Tempo.
