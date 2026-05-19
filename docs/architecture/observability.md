# Observability

WebhookWise is OTel-first:

```text
API / Worker / Scheduler
  -> OpenTelemetry SDK
  -> OTLP HTTP or gRPC
  -> Grafana Alloy
      -> Metrics: Prometheus-compatible backend
      -> Traces: Tempo / Jaeger
      -> Logs: Loki
  -> Pyroscope SDK
      -> Profiles: Pyroscope
Dashboard browser
  -> Grafana Faro Web SDK
  -> Alloy faro.receiver
      -> Logs: Loki
      -> Traces: Tempo
webhook-service container
  -> Grafana Beyla eBPF auto-instrumentation
  -> Alloy OTLP receiver
k6
  -> Prometheus remote write
      -> Dashboard / Alerting: Grafana
```

Application code only emits telemetry. It does not expose `/metrics`, write Loki directly, or depend on `prometheus_client`. Profiles are the one direct backend SDK integration because Python profile export is still more mature through Pyroscope than through a stable OTel profiles SDK.

## Application Signals

- Metrics: `core.metrics` compatibility facade backed by OTel Meter, received by Alloy and remote-written to Prometheus.
- Traces: `core.otel.span(...)` compatibility facade backed by OTel Tracer, exported through Alloy to Tempo.
- Logs: standard Python `logging`, structured JSON locally and OTLP logs when enabled. Alloy also tails `logs/*.log` into Loki for local debugging.
- Events: `core.otel.emit_event(...)`, emitted as span events plus structured log records with `event.name`.
- Profiles: optional Pyroscope continuous profiling via `PYROSCOPE_ENABLED=true`.
- Frontend RUM: Grafana Faro Web SDK is loaded by the Dashboard in local mode and posts to Alloy's `faro.receiver`.
- Auto-instrumentation: Grafana Beyla watches the API container with eBPF and emits HTTP/SQL/Redis metrics and traces over OTLP.
- Load testing: k6 sends synthetic webhook traffic and writes `k6_*` metrics to Prometheus remote write.
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

Metrics are emitted through `core.metrics`, exported over OTLP, converted by Alloy's Prometheus exporter, and remote-written into the local Prometheus. Metric labels are intentionally low-cardinality; use logs, traces, events, and signals for `event_id`, `request_id`, alert hash, and target URLs.

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

Operational dashboards should be built from these component metrics, then linked to traces, logs, profiles, events, and signals for detail. For example: start from `http_server_request_duration_*` or `worker_task_duration_*`, jump into Tempo traces by `trace_id`, inspect Loki logs with the same trace/span IDs, and use Pyroscope profiles when latency rises without an obvious dependency error. For dashboard coverage, No data rules, and PromQL maintenance notes, see [observability-dashboard.md](observability-dashboard.md). For a field guide that explains what each local metric means and how to interpret abnormal values, see [observability-local-lab.md](observability-local-lab.md#指标解释速查).

## Local Stack

For a step-by-step local learning flow with screenshots, see [observability-local-lab.md](observability-local-lab.md).

Start the default app stack plus local observability backends:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

Grafana is available at `http://localhost:3000` with Prometheus, Tempo, Loki, and Pyroscope datasources provisioned. The provisioned AIOps dashboard is described in [observability-dashboard.md](observability-dashboard.md). Alloy is available at `http://localhost:12345`, and the local Faro endpoint is `http://localhost:12347/collect`.

Pyroscope is available directly at `http://localhost:4040`. For practical reading notes on CPU cores, top table, flamegraphs, and common WebhookWise API / worker patterns, see [observability-local-lab.md](observability-local-lab.md#看-profile).

Run the k6 smoke load check:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile load run --rm k6
```

Beyla runs as a privileged sidecar sharing the API container PID namespace. It is useful for learning eBPF-based auto-instrumentation, but it requires Linux kernel/eBPF support from the Docker host.

Useful environment variables:

- `OTEL_ENABLED=true`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4318`
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_METRIC_EXPORT_INTERVAL=10000`
- `PYROSCOPE_ENABLED=true`
- `PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040`
- `PYROSCOPE_APPLICATION_NAME=webhookwise-api`
- `PYROSCOPE_SAMPLE_RATE=100`
- `PYROSCOPE_SPAN_PROFILES_ENABLED=true`
- `FARO_RECEIVER_PORT=12347`
- `K6_BASE_URL=http://webhook-service:8000`
