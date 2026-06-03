# Observability

WebhookWise is OTel-first:

```text
API / Worker / Scheduler
  -> OpenTelemetry SDK (logs, traces, metrics)
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

Application code only emits telemetry. It does not expose `/metrics`, tail application files into Loki, write Loki directly, or depend on `prometheus_client`. Profiles are the one direct backend SDK integration because Python profile export is still more mature through Pyroscope than through a stable OTel profiles SDK.

## Application Signals

- Metrics: `core.observability.metrics`, received by Alloy and remote-written to Prometheus.
- Traces: `core.observability.tracing.span(...)`, exported through Alloy to Tempo.
- Logs: standard Python `logging`, structured as the OTel log data model locally and exported as OTLP logs. `severity` is lowercase (`trace/debug/info/warn/error/fatal`), while `severity_text` keeps the uppercase display value. Logs carry `trace_id`, `span_id`, `trace_flags`, `logger.name`, resource attributes, and canonical domain attributes.
- Events: `core.observability.events.emit_event(...)`, emitted as span events plus structured log records with `event.name`.
- Profiles: optional Pyroscope continuous profiling via `PYROSCOPE_ENABLED=true`.
- Frontend RUM: Grafana Faro Web SDK is loaded by the Dashboard in local mode and posts to Alloy's `faro.receiver`.
- Auto-instrumentation: Grafana Beyla watches the API container with eBPF and emits HTTP/SQL/Redis metrics and traces over OTLP.
- Load testing: k6 sends synthetic webhook traffic and writes `k6_*` metrics to Prometheus remote write.
- Signals: `core.observability.signals.record_signal(...)`, low-cardinality state transitions for domain health and workflow outcomes.
- Export: OTLP only.

Canonical attributes include:

- `service.name`, `service.namespace`, `service.version`, `deployment.environment`
- `webhook.source`, `webhook.event_id`, `webhook.alert_hash`, `webhook.importance`, `webhook.status`
- `forward.target`, `forward.status`
- `ai.model`, `ai.provider`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
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
- Signals answer "what state did the system enter?" Use `record_signal` for state transitions such as `webhook.task=completed|error|suppressed` and `circuit_breaker=open|closed`.

The local stack pins OpenTelemetry semantic conventions to schema
`https://opentelemetry.io/schemas/1.41.0` through `OTEL_SCHEMA_URL` and
`OTEL_SEMCONV_VERSION`. Resource, tracer, meter, and stdout JSON logs all expose
the same schema intent so later semconv upgrades can be reviewed as explicit
schema migrations.

## Component Metric Catalog

Metrics are emitted through `core.observability.metrics`, exported over OTLP, converted by Alloy's Prometheus exporter, and remote-written into the local Prometheus. Metric labels are intentionally low-cardinality; use logs, traces, events, and signals for `event_id`, `request_id`, alert hash, and target URLs.

| Component | Metrics | Primary labels |
| --- | --- | --- |
| HTTP/API | OTel FastAPI auto metrics: `http.server.request.duration`, `http.server.request.body.size`, `http.server.active_requests` | `http.request.method`, `http.route`, `http.response.status_code` |
| Webhook ingress | `webhook.received`, `webhook.ingress.requests`, `webhook.ingress.payload.size` | `webhook.source`, `webhook.status`, `webhook.outcome` |
| Security | `security.checks` | `security.check`, `security.result` |
| Queue | `queue.operations`, `queue.operation.duration`, `queue.depth`, `queue.pending`, `queue.lag` | `queue.name`, `queue.operation`, `queue.status`, `queue.stream`, `queue.group`; `queue.depth` is retained Redis Stream length, while backlog is `queue.pending` / `queue.lag` |
| Worker/runtime | `worker.task.runs`, `worker.task.duration`, `webhook.running_tasks` | `worker.task.name`, `worker.task.status` |
| Webhook pipeline | `webhook.pipeline.steps`, `webhook.pipeline.step.duration`, `webhook.processing.duration`, `webhook.processed`, `webhook.dedup.decisions`, `webhook.dedup.duration`, `webhook.analysis.results` | `pipeline.step`, `webhook.source`, `webhook.outcome`, `webhook.status`, `dedup.action`, `webhook.route`, `webhook.importance`, `ai.degraded` |
| Noise reduction | `webhook.noise.evaluations`, `webhook.noise.evaluation.duration` | `webhook.source`, `webhook.relation`, `webhook.suppressed` |
| AI analysis | `ai.requests`, `ai.request.duration`, `ai.request.errors`, `ai.tokens`, `ai.cost`, `ai.cache.requests`, `ai.cache.operation.duration`, `ai.degradations` | `webhook.source`, `ai.engine`, `ai.status`, `ai.model`, `ai.token_type`, `ai.cache.operation`, `ai.cache.result` |
| Forwarding | `webhook.forward.decisions`, `forward.delivery`, `forward.delivery.duration`, `forward.outbox.records`, `forward.outbox.process.duration`, `forward.outbox.backlog.age` | `webhook.source`, `forward.decision`, `forward.reason`, `forward.target_type`, `forward.status` |
| Resilience | `circuit_breaker.state` | `circuit_breaker.name`, `circuit_breaker.state` |
| Database | `db.health.state`, `db.sessions`, `db.session.duration`, `db.pool.connections.checked_out`, `db.pool.connections.max`, `webhook.events.count`, `webhook.processing.status_count` | `db.state`, `db.operation`, `db.status`, `webhook.status` |
| Redis | `redis.operations`, `redis.operation.duration` | `redis.operation`, `redis.status` |
| Scheduler | `scheduler.task.runs`, `scheduler.task.duration`, `scheduler.task.lag`, `scheduler.task.last_success_unixtime` | `scheduler.task.name`, `scheduler.task.status` |
| Observability layer | `observability.events`, `observability.signals` | `event.name`, `signal.name`, `signal.state` |

Operational dashboards should be built from these component metrics, then linked to traces, logs, profiles, events, and signals for detail. For example: start from `http_server_request_duration_*` or `worker_task_duration_*`, jump into Tempo traces by `trace_id`, inspect Loki logs with the same trace/span IDs, and use Pyroscope profiles when latency rises without an obvious dependency error. Histogram exemplars are enabled with trace-based filtering so Grafana can jump from selected latency samples directly to Tempo. Prometheus loads `deploy/observability/prometheus/alerts.yml` and sends alerts to Alertmanager for SLO and dependency signals. For dashboard coverage, No data rules, and PromQL maintenance notes, see [dashboards.md](dashboards.md). For CLI, skill, MCP-style query tooling, and the end-to-end smoke check, see [query-tools.md](query-tools.md). For a field guide that explains what each local metric means and how to interpret abnormal values, see [local-lab/metrics.md](local-lab/metrics.md#指标解释速查).

## Local Stack

For a step-by-step local learning flow with screenshots, see [local-lab/README.md](local-lab/README.md).

Start the default app stack, then the local observability backends:

```bash
docker compose up -d --build
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml up -d --build
```

Grafana is available at `http://localhost:3000` with Prometheus, Tempo, Loki, and Pyroscope datasources provisioned. The provisioned AIOps dashboards are described in [dashboards.md](dashboards.md). Alertmanager is available at `http://localhost:9093`. Alloy is available at `http://localhost:12345`, and the local Faro endpoint is `http://localhost:12347/collect`.

Pyroscope is available directly at `http://localhost:4040`. For practical reading notes on CPU cores, top table, flamegraphs, and common WebhookWise API / worker patterns, see [local-lab/profiling.md](local-lab/profiling.md#看-profile).

Run the k6 smoke load check:

```bash
docker compose -p webhookwise-observability --env-file .env -f deploy/compose/docker-compose.observability.yml --profile load run --rm k6
```

Beyla runs as a privileged sidecar sharing the API container PID namespace. It is useful for learning eBPF-based auto-instrumentation, but it requires Linux kernel/eBPF support from the Docker host.

Useful environment variables:

- `OTEL_ENABLED=true`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317`
- `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`
- `OTEL_LOGS_ENABLED=true`; logs are exported through OTLP instead of Alloy file tailing.
- `OTEL_METRIC_EXPORT_INTERVAL=10000`
- `OTEL_SCHEMA_URL=https://opentelemetry.io/schemas/1.41.0`
- `OTEL_SEMCONV_VERSION=1.41.0`
- `OTEL_METRICS_EXEMPLAR_FILTER=trace_based`
- `OTEL_SEMCONV_STABILITY_OPT_IN=http`
- `OTEL_TRACES_SAMPLER=always_on` locally; production usually uses `parentbased_traceidratio`
- `OTEL_TRACES_SAMPLER_ARG=0.1` for 10% production head sampling
- The application honors `always_on`, `always_off`, `traceidratio`, `parentbased_traceidratio`, `parentbased_always_on`, and `parentbased_always_off`. Error-only or slow-only tail sampling belongs in the collector/backend because those decisions require span outcomes.
- `WEBHOOKWISE_SOURCE_LABEL_LIMIT=128` to cap custom `webhook.source` label cardinality; overflow is reported as `other`
- Local observability images are pinned in `.env.example.all` and `deploy/compose/docker-compose.observability.yml`; avoid `latest` so learning screenshots and contract tests stay reproducible.
- Alertmanager posts local alerts back to `http://webhook-service:8000/v1/webhook/alertmanager`; keep its receiver compatible with the current webhook auth settings.
- `PYROSCOPE_ENABLED=true`
- `PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040`
- `PYROSCOPE_APPLICATION_NAME=webhookwise-api`
- `PYROSCOPE_SAMPLE_RATE=100`
- `PYROSCOPE_SPAN_PROFILES_ENABLED=true`
- `FARO_RECEIVER_PORT=12347`
- `K6_BASE_URL=http://webhook-service:8000`

AI spans include GenAI semantic attributes so the same traces can later be
exported to Langfuse or another LLM observability backend through an OTLP
collector path. Keep prompt and completion bodies out of telemetry by default;
capture model, provider, token, cost, latency, and error metadata first.

On shutdown, API and worker processes call `shutdown_observability()` so the
BatchSpanProcessor, MetricReader, and optional log exporter flush buffered
telemetry before the process exits. TaskIQ webhook tasks also carry the incoming
W3C `traceparent` from API enqueue to worker execution, keeping request, worker,
database, forwarding, and outbox spans in the same trace.
