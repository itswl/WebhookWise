# Local Observability Lab Handbook: Logs, Traces, Smoke, and Alerts

[Back to overview](README.md)

## Viewing Logs

Grafana -> Explore -> select the `Loki` datasource.

All aggregated application logs:

```logql
{service_name="webhookwise"}
```

By service:

```logql
{service_name="webhookwise-api"}
{service_name="webhookwise-worker"}
{service_name="webhookwise-scheduler"}
```

By level:

```logql
{service_name="webhookwise-api", severity="error"}
```

Convention: the structured field `severity` always uses lowercase `trace/debug/info/warn/error/fatal`, which makes Loki querying and alerting easier; the log content also keeps `severity_text` with the uppercase value `TRACE/DEBUG/INFO/WARN/ERROR/FATAL`, which makes it easy to scan the level quickly in a Grafana line format or scrolling logs.

Application logs enter Alloy as OTLP logs. Alloy puts `severity`, `severity_text`, `event.name`, `signal.name`, `signal.state`, `webhook.source`, and `webhook.status` into Loki labels; the Loki side uses sanitized label names (for example `event_name`, `webhook_source`). `trace_id` / `span_id` are only used as log fields and derived-field jump hints, not as labels, to avoid high cardinality overwhelming the index.

By structured event:

```logql
{service_name="webhookwise-api", event_name!=""}
```

Logs usually contain `trace_id` / `span_id`, which you can use to jump to Tempo to inspect the trace.

![Service logs in Loki](assets/service-logs-loki.jpg)

Infrastructure container logs are currently not collected into Loki in a unified way; view them with Compose service logs:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 alloy
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 prometheus
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 loki
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 tempo
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 pyroscope
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml logs --tail=100 grafana
docker compose logs --tail=100 postgres
docker compose logs --tail=100 redis
```

## Viewing Traces

Grafana -> Explore -> select the `Tempo` datasource.

Common searches:

```text
service.name = webhookwise-api
service.name = webhookwise-worker
service.name = webhookwise-scheduler
```

If a log contains a `trace_id`, you can open it directly by trace id in Tempo. The Grafana datasource is configured with `tracesToLogsV2` and `tracesToProfiles`, so you can jump from a trace to Loki logs and Pyroscope profiles.

Jumping back from Loki to Tempo: the Loki datasource is configured with a derived field that extracts the 32-character trace id from the JSON log's
`trace_id`; click `View Trace` to open Tempo directly.

Jumping from Tempo to Loki: the Tempo datasource's `tracesToLogsV2` maps
`service.name -> service_name`, `webhook.source -> webhook_source`, and
`webhook.status -> webhook_status` to Loki labels and enables trace id filtering.

The Tempo API can also quickly confirm data:

```bash
curl -fsS 'http://localhost:3200/api/search?tags=service.name%3Dwebhookwise-api&limit=5'
```

## Smoke and Alerts

After changing the observability configuration, run an end-to-end smoke first:

```bash
python scripts/observability/webhookwise_observe.py smoke
```

It checks health status, sends one `observability-smoke` webhook, and then confirms that Prometheus,
Loki, Tempo, and the Prometheus alert rules all respond at a basic level. In production, only query without generating traffic:

```bash
python scripts/observability/webhookwise_observe.py smoke --skip-webhook
```

The local Prometheus loads `deploy/observability/prometheus/alerts.yml`. This rule set includes:

- SLO recording rules for API / ingress / processing / forward and 5m+1h / 30m+6h burn-rate alerts
- API 5xx ratio
- webhook dead letter
- queue pending / lag backlog
- continued growth of Redis Stream retained depth
- DB pool approaching capacity
- AI errors and high latency
- Alloy exporter queue congestion
- Loki write drops
- Alloy config load failures

The same rule file also provides recording rules that record the easily misunderstood `_ratio` gauge names as
more direct names, for example `queue_pending`, `queue_lag`, `queue_depth`,
`webhook_events_active`, and `db_pool_connections_checked_out`.
When troubleshooting, you can use `python scripts/observability/webhookwise_observe.py runbook <alert_name>`
to automatically collect the alert state, related SLO/RED/USE queries, error logs, and trace and profile links.
