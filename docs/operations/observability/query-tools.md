# Observability Query Tools

This repository includes a small query toolkit for WebhookWise runtime data.
It is designed for Codex/agents first, but it also works as a plain CLI.

## CLI

Run from the repository root:

```bash
python scripts/observability/webhookwise_observe.py health
```

Common commands:

```bash
# List named PromQL presets
python scripts/observability/webhookwise_observe.py preset --list

# Run a preset
python scripts/observability/webhookwise_observe.py preset api-rate
python scripts/observability/webhookwise_observe.py preset queue-backlog
python scripts/observability/webhookwise_observe.py preset collector-health

# Run custom PromQL
python scripts/observability/webhookwise_observe.py promql 'sum by (http_response_status_code) (increase(http_server_request_duration_seconds_count[1h]))'

# Query Loki logs
python scripts/observability/webhookwise_observe.py logs --query '{service_name="webhookwise"} | json' --limit 20

# Search recent Tempo traces
python scripts/observability/webhookwise_observe.py tempo --service-name webhookwise-api --limit 5

# Build Pyroscope profile links
python scripts/observability/webhookwise_observe.py profiles --service-name webhookwise-api

# Validate every PromQL expression in the provisioned dashboard
python scripts/observability/webhookwise_observe.py dashboard --validate

# Run an end-to-end telemetry smoke check
python scripts/observability/webhookwise_observe.py smoke

# Run offline contract checks used by CI
python scripts/observability/webhookwise_observe.py contract

# Run a full runtime acceptance check
python scripts/observability/webhookwise_observe.py acceptance --run-k6

# Gather a compact alert runbook summary
python scripts/observability/webhookwise_observe.py runbook WebhookWiseApiAvailabilityFastBurn
```

Use `--json` on commands when another program or agent should consume the output.

## Online Grafana Proxy Mode

For production, prefer querying through Grafana's datasource proxy instead of
exposing Prometheus or Loki directly.

Set the online endpoint and credentials in your shell:

```bash
export WEBHOOKWISE_QUERY_MODE=grafana-proxy
export WEBHOOKWISE_GRAFANA_URL=https://webhook-grafana.wetalk.eu.org
export WEBHOOKWISE_GRAFANA_TOKEN='<grafana-service-account-token>'
```

If the Grafana instance uses basic auth instead of a service account token:

```bash
export WEBHOOKWISE_GRAFANA_USER='<user>'
export WEBHOOKWISE_GRAFANA_PASSWORD='<password>'
```

Discover datasource UIDs first:

```bash
python scripts/observability/webhookwise_observe.py datasources
```

Then set the datasource UIDs when they differ from the defaults:

```bash
export WEBHOOKWISE_PROMETHEUS_DATASOURCE_UID=prometheus
export WEBHOOKWISE_LOKI_DATASOURCE_UID=loki
export WEBHOOKWISE_TEMPO_DATASOURCE_UID=tempo
export WEBHOOKWISE_PYROSCOPE_DATASOURCE_UID=pyroscope
```

Run the same commands against production:

```bash
python scripts/observability/webhookwise_observe.py health
python scripts/observability/webhookwise_observe.py preset api-rate
python scripts/observability/webhookwise_observe.py logs --query '{service_name="webhookwise-api"} | json' --limit 20
python scripts/observability/webhookwise_observe.py tempo --service-name webhookwise-api --limit 5
python scripts/observability/webhookwise_observe.py profiles --service-name webhookwise-api
python scripts/observability/webhookwise_observe.py dashboard --remote --uid webhook-wise-aiops
python scripts/observability/webhookwise_observe.py dashboard --validate
python scripts/observability/webhookwise_observe.py smoke --skip-webhook
python scripts/observability/webhookwise_observe.py runbook WebhookWiseProcessingSuccessFastBurn --since 7200
```

Do not commit tokens or passwords. Keep them in your shell, password manager, or
local ignored environment files.

## MCP-Style Stdio Server

Start the dependency-free JSON-RPC stdio server:

```bash
python scripts/observability/webhookwise_mcp.py
```

It exposes these tools:

| Tool | Purpose |
| --- | --- |
| `webhookwise_health` | Check API, Prometheus, Loki, Tempo, Grafana, Pyroscope, and Alloy readiness |
| `webhookwise_datasources` | List Grafana datasource names and UIDs |
| `webhookwise_promql` | Run an arbitrary PromQL instant query |
| `webhookwise_preset` | Run one of the named WebhookWise PromQL presets |
| `webhookwise_logs` | Run a Loki `query_range` |
| `webhookwise_tempo_search` | Search recent Tempo traces for a service |
| `webhookwise_profiles` | Build Pyroscope profile selectors and Grafana/Pyroscope links |
| `webhookwise_dashboard_validate` | Validate `deploy/observability/grafana/dashboards/dashboard.json` against Prometheus |
| `webhookwise_smoke` | Run the API -> Prometheus -> Loki -> Tempo smoke check |
| `webhookwise_acceptance` | Run the full runtime acceptance checklist |
| `webhookwise_contract` | Run offline telemetry contract checks |
| `webhookwise_runbook` | Collect alert state, related PromQL, Loki errors, Tempo traces, and profile links |

Example JSON-RPC call:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"webhookwise_preset","arguments":{"name":"api-rate"}}}
```

## Codex Skill

A repo-local skill is stored at:

```text
.codex/skills/webhookwise-observability/SKILL.md
```

It tells Codex how to use the CLI and MCP-style entrypoint for questions such
as "why is the dashboard No data?", "check API latency", "show worker queue
lag", or "query recent Loki errors".

## Endpoints

Defaults target the local compose stack:

| Backend | Default URL |
| --- | --- |
| API | `http://localhost:8000` |
| Prometheus | `http://localhost:9090` |
| Loki | `http://localhost:3100` |
| Tempo | `http://localhost:3200` |
| Grafana | `http://localhost:3000` |
| Pyroscope | `http://localhost:4040` |
| Alloy | `http://localhost:12345` |

Override with:

```bash
WEBHOOKWISE_PROMETHEUS_URL=http://...
WEBHOOKWISE_LOKI_URL=http://...
WEBHOOKWISE_TEMPO_URL=http://...
WEBHOOKWISE_GRAFANA_URL=http://...
WEBHOOKWISE_PYROSCOPE_URL=http://...
WEBHOOKWISE_ALLOY_URL=http://...
WEBHOOKWISE_API_URL=http://...
WEBHOOKWISE_QUERY_MODE=direct
WEBHOOKWISE_GRAFANA_USER=admin
WEBHOOKWISE_GRAFANA_PASSWORD=admin
WEBHOOKWISE_GRAFANA_TOKEN=...
WEBHOOKWISE_PROMETHEUS_DATASOURCE_UID=prometheus
WEBHOOKWISE_LOKI_DATASOURCE_UID=loki
WEBHOOKWISE_TEMPO_DATASOURCE_UID=tempo
WEBHOOKWISE_HTTP_USER_AGENT=...
```

The default user agent is `WebhookWise-Observability/0.1`; override it only
when a front door requires a different client identity.

The `smoke` command posts a synthetic webhook by default. Use `--skip-webhook`
when you only want to query online telemetry and do not want to create traffic.
If webhook auth is enabled, export `WEBHOOK_SECRET` before running the command so
the synthetic request can be signed.

`contract` is offline and safe for CI. It checks dashboard/rule metric coverage,
basic PromQL balance, stale metric names, Loki label cardinality, schema URL
consistency, structured logging helpers, and sensitive log labels.

`acceptance` is runtime-oriented. It extends `smoke` with Grafana datasource
checks, dashboard query validation, SLO recording-rule queries, and histogram
presence. Add `--run-k6` when you want fresh synthetic load before checking, and
`--strict` when warnings should fail the command.

`runbook <alert_name>` is for active incidents. It pulls the alert state, related
SLO/RED/USE PromQL, recent error logs, recent traces for the likely service, and
a Pyroscope/Grafana profile link.

## Preset Groups

| Area | Presets |
| --- | --- |
| API | `api-rate`, `api-latency-p95`, `api-5xx-rate` |
| Webhook | `webhook-rate`, `active-events`, `noise-rate`, `suppression-rate` |
| Queue / worker | `queue-backlog`, `queue-retained-depth`, `queue-ops`, `worker-runs`, `worker-latency-p95` |
| DB / Redis | `db-pool`, `db-latency-p95`, `redis-latency-p95` |
| Scheduler | `scheduler-lag`, `scheduler-last-success-age` |
| SLO | `slo-api-success`, `slo-ingress-success`, `slo-processing-success`, `slo-forward-success`, `slo-ai-degradation`, `slo-db-utilization`, `slo-queue-backlog` |
| AI / forwarding | `ai-latency-p95`, `ai-cost`, `ai-tokens`, `ai-cache-rate`, `ai-cache-latency-p95`, `deep-analysis-rate`, `forward-rate`, `forward-outbox-rate`, `forward-outbox-latency-p95`, `forward-outbox-backlog-age`, `circuit-breaker-state` |
| Deep diagnostics | `webhook-status`, `pipeline-step-latency-p95`, `queue-operation-latency-p95`, `webhook-payload-p95`, `noise-evaluations`, `noise-latency-p95` |
| Frontend / eBPF / load / collector | `faro-rum`, `beyla-calls`, `k6-smoke`, `collector-health`, `environment-services`, `process-memory`, `service-graph-rate`, `service-graph-failures`, `collector-queue`, `loki-write-latency-p95`, `loki-write-retries` |

## Maintenance

When metrics change:

1. Update `scripts/observability/query_lib.py` presets.
2. Update `.codex/skills/webhookwise-observability/SKILL.md` if the workflow changes.
3. Update `deploy/observability/grafana/dashboards/dashboard.json` if the dashboard should reflect the change.
4. Update `deploy/observability/alerts.yml` if the new metric affects SLOs or alerting.
5. Run:

```bash
python scripts/observability/webhookwise_observe.py dashboard --validate
python scripts/observability/webhookwise_observe.py smoke
python scripts/observability/webhookwise_observe.py contract
```
