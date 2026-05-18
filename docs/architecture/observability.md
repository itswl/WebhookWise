# Observability

WebhookWise is OTel-first:

```text
API / Worker / Scheduler
  -> OpenTelemetry SDK
  -> OTLP HTTP or gRPC
  -> OpenTelemetry Collector
      -> Metrics: Prometheus-compatible backend
      -> Traces: Tempo / Jaeger
      -> Logs: Loki
  -> Pyroscope SDK
      -> Profiles: Pyroscope
      -> Dashboard / Alerting: Grafana
```

Application code only emits telemetry. It does not expose `/metrics`, write Loki directly, or depend on `prometheus_client`. Profiles are the one direct backend SDK integration because Python profile export is still more mature through Pyroscope than through a stable OTel profiles SDK.

## Application Signals

- Metrics: `core.metrics` compatibility facade backed by OTel Meter.
- Traces: `core.otel.span(...)` compatibility facade backed by OTel Tracer.
- Logs: standard Python `logging`, structured JSON locally and OTLP logs when enabled.
- Events: `core.otel.emit_event(...)`, emitted as span events plus structured log records with `event.name`.
- Profiles: optional Pyroscope continuous profiling via `PYROSCOPE_ENABLED=true`.
- Signals: `core.otel.record_signal(...)`, low-cardinality state transitions for domain health and workflow outcomes.
- Export: OTLP only.

Canonical attributes include:

- `service.name`, `service.version`, `deployment.environment`
- `webhook.source`, `webhook.event_id`, `webhook.alert_hash`, `webhook.importance`, `webhook.status`
- `forward.target`, `forward.status`
- `ai.model`, `ai.provider`
- `retry.count`, `error.type`
- `event.name`
- `signal.name`, `signal.state`

## Modern Signal Map

WebhookWise treats the old "three pillars" as a baseline and adds three production-facing layers:

- Logs answer "what happened?" Keep them structured and correlated with trace/span IDs.
- Metrics answer "how much/how often/how slow?" Keep labels low-cardinality.
- Traces answer "where did a request spend time?" Use spans around HTTP, Redis, DB, AI, and forwarding.
- Profiles answer "which code consumed CPU or memory over time?" Pyroscope is enabled in the local observability compose stack.
- Events answer "which discrete operational or product event happened?" Use `emit_event` for workflow milestones such as `webhook.task.started`, `webhook.analysis.completed`, and `webhook.storm.suppressed`.
- Signals answer "what state did the system enter?" Use `record_signal` for state transitions such as `webhook.task=completed|error|suppressed`.

## Component Metric Catalog

Metrics are emitted through `core.metrics`, exported over OTLP, converted by the collector's Prometheus exporter, and namespaced as `webhookwise_*` in the local stack. Metric labels are intentionally low-cardinality; use logs, traces, events, and signals for `event_id`, `request_id`, alert hash, and target URLs.

| Component | Metrics | Primary labels |
| --- | --- | --- |
| HTTP/API | `http.server.requests`, `http.server.request.duration`, `http.server.request.body.size` | `http.method`, `http.route`, `http.status_code` |
| Webhook ingress | `webhook.received`, `webhook.ingress.payload.size` | `webhook.source`, `webhook.status`, `webhook.outcome` |
| Security | `security.checks` | `security.check`, `security.result` |
| Queue | `queue.operations`, `queue.operation.duration`, `queue.depth`, `queue.pending`, `queue.lag` | `queue.name`, `queue.operation`, `queue.status`, `queue.stream`, `queue.group` |
| Worker/runtime | `worker.task.runs`, `worker.task.duration`, `webhook.running_tasks`, `webhook.semaphore.timeouts` | `worker.task.name`, `worker.task.status` |
| Webhook pipeline | `webhook.pipeline.steps`, `webhook.pipeline.step.duration`, `webhook.processing.duration`, `webhook.processed` | `pipeline.step`, `webhook.source`, `webhook.outcome`, `webhook.status` |
| Noise reduction | `webhook.noise.evaluations`, `webhook.noise.evaluation.duration`, `webhook.suppressed` | `webhook.source`, `webhook.relation`, `webhook.suppressed` |
| AI analysis | `ai.request.duration`, `ai.request.errors`, `ai.tokens`, `ai.cost`, `ai.cache.requests`, `ai.cache.operation.duration`, `ai.degradations` | `ai.engine`, `ai.model`, `ai.token_type`, `ai.cache.operation`, `ai.cache.result` |
| Forwarding | `forward.delivery`, `forward.delivery.duration`, `forward.retry`, `forward.outbox.records`, `forward.outbox.process.duration` | `forward.target_type`, `forward.status` |
| Database | `db.sessions`, `db.session.duration`, `db.pool.connections.checked_out`, `db.pool.connections.max`, `webhook.events.count`, `webhook.processing.status_count`, `webhook.stuck.status_count` | `db.operation`, `db.status`, `webhook.status` |
| Redis | `redis.operations`, `redis.operation.duration` | `redis.operation`, `redis.status` |
| Scheduler | `scheduler.task.runs`, `scheduler.task.duration`, `scheduler.task.lag`, `scheduler.task.last_success_unixtime` | `scheduler.task.name`, `scheduler.task.status` |
| Observability layer | `observability.events`, `observability.signals` | `event.name`, `signal.name`, `signal.state` |

Operational dashboards should be built from these component metrics, then linked to traces, logs, profiles, events, and signals for detail. For example: start from `webhookwise_http_server_request_duration_*` or `webhookwise_worker_task_duration_*`, jump into Tempo traces by `trace_id`, inspect Loki logs with the same trace/span IDs, and use Pyroscope profiles when latency rises without an obvious dependency error.

## Local Stack

Start the default app stack plus local observability backends:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

Grafana is available at `http://localhost:3000` with Prometheus, Tempo, Loki, and Pyroscope datasources provisioned.

Pyroscope is available directly at `http://localhost:4040`.

Useful environment variables:

- `OTEL_ENABLED=true`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_METRIC_EXPORT_INTERVAL=10000`
- `PYROSCOPE_ENABLED=true`
- `PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040`
- `PYROSCOPE_APPLICATION_NAME=webhookwise-api`
- `PYROSCOPE_SAMPLE_RATE=100`
- `PYROSCOPE_SPAN_PROFILES_ENABLED=true`
