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
      -> Dashboard / Alerting: Grafana
```

Application code only emits telemetry. It does not expose `/metrics`, write Loki directly, depend on `prometheus_client`, or bind to a vendor APM SDK.

## Application Signals

- Metrics: `core.metrics` compatibility facade backed by OTel Meter.
- Traces: `core.otel.span(...)` compatibility facade backed by OTel Tracer.
- Logs: standard Python `logging`, structured JSON locally and OTLP logs when enabled.
- Export: OTLP only.

Canonical attributes include:

- `service.name`, `service.version`, `deployment.environment`
- `webhook.source`, `webhook.event_id`, `webhook.alert_hash`, `webhook.importance`, `webhook.status`
- `forward.target`, `forward.status`
- `ai.model`, `ai.provider`
- `retry.count`, `error.type`

## Local Stack

Start the default app stack plus local observability backends:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

Grafana is available at `http://localhost:3000` with Prometheus, Tempo, and Loki datasources provisioned.
