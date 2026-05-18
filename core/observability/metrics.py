"""OpenTelemetry metric instruments with a small legacy-compatible facade."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from core.observability.attributes import normalize_attribute_value
from core.observability.exporters import build_metric_exporter, otel_enabled
from core.observability.resource import build_resource

_provider_initialized = False
_meter_provider_lock = threading.Lock()


def setup_meter_provider(*, service_name: str | None = None) -> None:
    global _provider_initialized
    if _provider_initialized or not otel_enabled():
        return
    with _meter_provider_lock:
        if _provider_initialized or not otel_enabled():
            return
        exporter = build_metric_exporter()
        if exporter is None:
            logging.getLogger("webhook_service").warning("[OTEL] metrics enabled but no metric exporter is configured")
            _provider_initialized = True
            return
        try:
            from opentelemetry import metrics
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        except ImportError:
            return
        reader = PeriodicExportingMetricReader(exporter)
        metrics.set_meter_provider(MeterProvider(resource=build_resource(service_name), metric_readers=[reader]))
        _provider_initialized = True


def _get_meter() -> Any | None:
    if not otel_enabled():
        return None
    setup_meter_provider()
    try:
        from opentelemetry import metrics
    except ImportError:
        return None
    return metrics.get_meter("webhookwise")


def _attrs_key(attributes: Mapping[str, str | bool | int | float]) -> tuple[tuple[str, str | bool | int | float], ...]:
    return tuple(sorted(attributes.items()))


def _alias_for_key(key: str, label_keys: tuple[str, ...]) -> str:
    if key in label_keys:
        return key
    suffix_matches = [label for label in label_keys if label.endswith(f".{key}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    if key == "token_type":
        return "ai.token_type" if "ai.token_type" in label_keys else key
    alias_map = {
        "source": "webhook.source",
        "event_id": "webhook.event_id",
        "alert_hash": "webhook.alert_hash",
        "importance": "webhook.importance",
        "relation": "webhook.relation",
        "suppressed": "webhook.suppressed",
        "outcome": "webhook.outcome",
        "status": "webhook.status",
        "name": "scheduler.task.name",
        "model": "ai.model",
        "provider": "ai.provider",
        "engine": "ai.engine",
        "type": "error.type",
        "field": "webhook.field",
        "reason": "error.reason",
        "stream": "queue.stream",
        "group": "queue.group",
    }
    aliased = alias_map.get(key, key)
    if aliased in label_keys:
        return aliased
    return key


class BoundMetric:
    def __init__(self, metric: _MetricBase, attributes: dict[str, str | bool | int | float]) -> None:
        self._metric = metric
        self._attributes = attributes

    def inc(self, amount: int | float = 1) -> None:
        self._metric.inc(amount, self._attributes)

    def dec(self, amount: int | float = 1) -> None:
        self._metric.dec(amount, self._attributes)

    def set(self, value: int | float) -> None:
        self._metric.set(value, self._attributes)

    def observe(self, value: int | float) -> None:
        self._metric.observe(value, self._attributes)


class _MetricBase:
    def __init__(
        self,
        name: str,
        description: str,
        label_keys: Iterable[str] = (),
        *,
        unit: str = "1",
    ) -> None:
        self.name = name
        self.description = description
        self.label_keys = tuple(label_keys)
        self.unit = unit
        self._instrument: Any | None = None
        self._lock = threading.Lock()

    def labels(self, *values: object, **labels: object) -> BoundMetric:
        attributes: dict[str, str | bool | int | float] = {}
        for key, value in zip(self.label_keys, values, strict=False):
            if value is not None:
                attributes[key] = normalize_attribute_value(value)
        for key, value in labels.items():
            if value is None:
                continue
            attributes[_alias_for_key(key, self.label_keys)] = normalize_attribute_value(value)
        return BoundMetric(self, attributes)

    def _get_or_create(self, factory: Callable[[Any], Any]) -> Any | None:
        if self._instrument is not None:
            return self._instrument
        meter = _get_meter()
        if meter is None:
            return None
        with self._lock:
            if self._instrument is None:
                self._instrument = factory(meter)
            return self._instrument

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        raise NotImplementedError


class Counter(_MetricBase):
    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        if amount <= 0:
            return
        instrument = self._get_or_create(
            lambda meter: meter.create_counter(self.name, description=self.description, unit=self.unit)
        )
        if instrument is not None:
            instrument.add(amount, attributes=dict(attributes or {}))

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return


class Histogram(_MetricBase):
    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        instrument = self._get_or_create(
            lambda meter: meter.create_histogram(self.name, description=self.description, unit=self.unit)
        )
        if instrument is not None:
            instrument.record(value, attributes=dict(attributes or {}))

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.observe(amount, attributes)

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        return

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.observe(value, attributes)


class Gauge(_MetricBase):
    def __init__(
        self,
        name: str,
        description: str,
        label_keys: Iterable[str] = (),
        *,
        unit: str = "1",
    ) -> None:
        super().__init__(name, description, label_keys, unit=unit)
        self._values: dict[tuple[tuple[str, str | bool | int | float], ...], int | float] = {}

    def _observe(self, options: object) -> list[Any]:
        try:
            from opentelemetry.metrics import Observation
        except ImportError:
            return []
        with self._lock:
            return [Observation(value, dict(attrs)) for attrs, value in self._values.items()]

    def _ensure_observable(self) -> None:
        self._get_or_create(
            lambda meter: meter.create_observable_gauge(
                self.name,
                callbacks=[self._observe],
                description=self.description,
                unit=self.unit,
            )
        )

    def set(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        with self._lock:
            self._values[_attrs_key(attributes or {})] = value
        self._ensure_observable()

    def inc(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount
        self._ensure_observable()

    def dec(self, amount: int | float = 1, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        key = _attrs_key(attributes or {})
        with self._lock:
            self._values[key] = self._values.get(key, 0) - amount
        self._ensure_observable()

    def observe(self, value: int | float, attributes: Mapping[str, str | bool | int | float] | None = None) -> None:
        self.set(value, attributes)


KNOWN_SOURCES: set[str] = {
    "github",
    "gitlab",
    "bitbucket",
    "cloud-monitor",
    "alert-system",
    "k8s-cluster",
    "production-server",
    "payment-system",
    "openclaw",
    "feishu-test",
    "production",
    "datadog",
    "grafana",
    "pagerduty",
    "prometheus",
    "sentry",
}


def sanitize_source(source: str) -> str:
    if not source:
        return "unknown"
    normalized = source.lower().strip()
    if normalized in KNOWN_SOURCES:
        return normalized
    return "unknown"


def setup_metrics(app: object | None = None) -> None:
    setup_meter_provider()
    update_db_pool_metrics()


def start_background_metrics_server() -> None:
    setup_metrics()


def update_db_pool_metrics() -> None:
    try:
        from db.session import get_db_pool_capacity, get_engine

        engine = get_engine()
        if engine is None:
            return
        cap = get_db_pool_capacity(engine)
        if cap is not None:
            DB_POOL_SIZE.set(cap)
    except (AttributeError, RuntimeError) as e:
        logging.getLogger("webhook_service").warning("[Metrics] unable to refresh DB pool capacity: %s", e)


WEBHOOK_RECEIVED_TOTAL = Counter(
    "webhook.received",
    "Total number of webhooks received",
    ("webhook.source", "webhook.status"),
)
HTTP_SERVER_REQUESTS_TOTAL = Counter(
    "http.server.requests",
    "HTTP server request count",
    ("http.method", "http.route", "http.status_code"),
)
HTTP_SERVER_REQUEST_DURATION_SECONDS = Histogram(
    "http.server.request.duration",
    "HTTP server request duration",
    ("http.method", "http.route", "http.status_code"),
    unit="s",
)
HTTP_SERVER_REQUEST_BODY_BYTES = Histogram(
    "http.server.request.body.size",
    "HTTP request body size from Content-Length",
    ("http.method", "http.route"),
    unit="By",
)
SECURITY_CHECKS_TOTAL = Counter(
    "security.checks",
    "Security check decision count",
    ("security.check", "security.result"),
)
WEBHOOK_INGRESS_PAYLOAD_BYTES = Histogram(
    "webhook.ingress.payload.size",
    "Webhook ingress payload size",
    ("webhook.source", "webhook.outcome"),
    unit="By",
)
WEBHOOK_PROCESSING_STATUS_TOTAL = Counter(
    "webhook.processed",
    "Webhook processing status transitions total",
    ("webhook.status",),
)
WEBHOOK_PIPELINE_STEP_TOTAL = Counter(
    "webhook.pipeline.steps",
    "Webhook pipeline step count",
    ("pipeline.step", "webhook.source", "webhook.outcome"),
)
WEBHOOK_PIPELINE_STEP_DURATION_SECONDS = Histogram(
    "webhook.pipeline.step.duration",
    "Webhook pipeline step duration",
    ("pipeline.step", "webhook.source", "webhook.outcome"),
    unit="s",
)
WEBHOOK_NOISE_REDUCED_TOTAL = Counter(
    "webhook.suppressed",
    "Number of webhooks evaluated by noise reduction",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
)
WEBHOOK_NOISE_EVALUATIONS_TOTAL = Counter(
    "webhook.noise.evaluations",
    "Noise-reduction evaluation count",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
)
WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS = Histogram(
    "webhook.noise.evaluation.duration",
    "Noise-reduction evaluation duration",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
    unit="s",
)
AI_TOKENS_TOTAL = Counter(
    "ai.tokens",
    "Total number of tokens consumed by AI analysis",
    ("ai.model", "ai.token_type"),
)
AI_COST_USD_TOTAL = Counter("ai.cost", "Total estimated cost of AI analysis in USD", ("ai.model",), unit="USD")
AI_CACHE_REQUESTS_TOTAL = Counter(
    "ai.cache.requests",
    "AI analysis cache request count",
    ("ai.cache.operation", "ai.cache.result"),
)
AI_CACHE_OPERATION_DURATION_SECONDS = Histogram(
    "ai.cache.operation.duration",
    "AI analysis cache operation duration",
    ("ai.cache.operation", "ai.cache.result"),
    unit="s",
)
AI_DEGRADATIONS_TOTAL = Counter(
    "ai.degradations",
    "AI analysis degradation count",
    ("ai.degradation.reason",),
)
AI_ANALYSIS_DURATION_SECONDS = Histogram(
    "ai.request.duration",
    "Time spent on AI analysis",
    ("webhook.source", "ai.engine"),
    unit="s",
)
WEBHOOK_PROCESSING_DURATION_SECONDS = Histogram(
    "webhook.processing.duration",
    "Time spent from start of pipeline to finish",
    ("webhook.source", "webhook.outcome"),
    unit="s",
)
OPENAI_ERRORS_TOTAL = Counter("ai.request.errors", "AI provider call errors total", ("error.type",))
ALERT_NUMERIC_PARSE_FAILURE_TOTAL = Counter(
    "webhook.parse.failures",
    "Alert numeric field parse failures during rule analysis",
    ("webhook.source", "webhook.field", "error.reason"),
)
SCHEDULED_TASK_RUNS_TOTAL = Counter(
    "scheduler.task.runs",
    "Scheduled task execution count",
    ("scheduler.task.name", "scheduler.task.status"),
)
SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME = Gauge(
    "scheduler.task.last_success_unixtime",
    "Last successful scheduled task execution unix time",
    ("scheduler.task.name",),
    unit="s",
)
SCHEDULED_TASK_LAG_SECONDS = Gauge(
    "scheduler.task.lag",
    "Scheduled task lag relative to its expected interval",
    ("scheduler.task.name",),
    unit="s",
)
SCHEDULED_TASK_DURATION_SECONDS = Histogram(
    "scheduler.task.duration",
    "Scheduled task duration",
    ("scheduler.task.name",),
    unit="s",
)
WORKER_TASKS_TOTAL = Counter(
    "worker.task.runs",
    "Worker task execution count",
    ("worker.task.name", "worker.task.status"),
)
WORKER_TASK_DURATION_SECONDS = Histogram(
    "worker.task.duration",
    "Worker task execution duration",
    ("worker.task.name", "worker.task.status"),
    unit="s",
)
FORWARD_RETRY_TOTAL = Counter("forward.retry", "Forward retry result count", ("forward.status",))
FORWARD_DELIVERY_TOTAL = Counter(
    "forward.delivery",
    "Forward delivery attempt count",
    ("forward.target_type", "forward.status"),
)
FORWARD_DELIVERY_DURATION_SECONDS = Histogram(
    "forward.delivery.duration",
    "Forward delivery attempt duration",
    ("forward.target_type", "forward.status"),
    unit="s",
)
FORWARD_OUTBOX_RECORDS_TOTAL = Counter(
    "forward.outbox.records",
    "Forwarding outbox lifecycle count",
    ("forward.target_type", "forward.status"),
)
FORWARD_OUTBOX_PROCESS_DURATION_SECONDS = Histogram(
    "forward.outbox.process.duration",
    "Forwarding outbox processing duration",
    ("forward.target_type", "forward.status"),
    unit="s",
)
DEEP_ANALYSIS_TOTAL = Counter("ai.deep_analysis", "Deep analysis task result count", ("webhook.status", "ai.engine"))
DATABASE_EVENTS_COUNT = Gauge("webhook.events.count", "Current number of webhook events in active table")
WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL = Counter(
    "webhook.semaphore.timeouts",
    "Semaphore acquisition timeout count",
)
WEBHOOK_STORM_SUPPRESSED_TOTAL = Counter(
    "webhook.storm.suppressed",
    "Webhook storm fail-fast suppression count",
    ("webhook.source",),
)
WEBHOOK_RECOVERY_POLLED_TOTAL = Counter("webhook.recovery.polled", "Recovered zombie event count")
WEBHOOK_RUNNING_TASKS = Gauge("webhook.running_tasks", "Currently running webhook processing tasks")
WEBHOOK_DEAD_LETTER_TOTAL = Counter("webhook.dead_letter", "Non-retryable dead letter event count")
WEBHOOK_PROCESSING_STATUS_COUNT = Gauge(
    "webhook.processing.status_count",
    "Webhook event count by processing status",
    ("webhook.status",),
)
WEBHOOK_STUCK_STATUS_COUNT = Gauge(
    "webhook.stuck.status_count",
    "Stuck webhook event count by processing status",
    ("webhook.status",),
)
WEBHOOK_MQ_STREAM_LENGTH = Gauge("queue.depth", "Webhook Redis Stream length", ("queue.stream",))
WEBHOOK_MQ_GROUP_PENDING = Gauge(
    "queue.pending",
    "Webhook Redis Stream consumer group pending count",
    ("queue.stream", "queue.group"),
)
WEBHOOK_MQ_GROUP_LAG = Gauge(
    "queue.lag",
    "Webhook Redis Stream consumer group lag",
    ("queue.stream", "queue.group"),
)
QUEUE_OPERATIONS_TOTAL = Counter(
    "queue.operations",
    "Queue operation count",
    ("queue.name", "queue.operation", "queue.status"),
)
QUEUE_OPERATION_DURATION_SECONDS = Histogram(
    "queue.operation.duration",
    "Queue operation duration",
    ("queue.name", "queue.operation", "queue.status"),
    unit="s",
)
DB_POOL_CHECKED_OUT = Gauge("db.pool.connections.checked_out", "Checked-out database connections")
DB_POOL_SIZE = Gauge("db.pool.connections.max", "Database connection pool capacity")
DB_SESSION_TOTAL = Counter(
    "db.sessions",
    "Database session/transaction lifecycle count",
    ("db.operation", "db.status"),
)
DB_SESSION_DURATION_SECONDS = Histogram(
    "db.session.duration",
    "Database session/transaction duration",
    ("db.operation", "db.status"),
    unit="s",
)
REDIS_OPERATIONS_TOTAL = Counter(
    "redis.operations",
    "Redis operation count",
    ("redis.operation", "redis.status"),
)
REDIS_OPERATION_DURATION_SECONDS = Histogram(
    "redis.operation.duration",
    "Redis operation duration",
    ("redis.operation", "redis.status"),
    unit="s",
)
OBSERVABILITY_EVENTS_TOTAL = Counter(
    "observability.events",
    "Structured observability events emitted by the application",
    ("event.name",),
)
OBSERVABILITY_SIGNAL_TOTAL = Counter(
    "observability.signals",
    "Domain signal state transitions emitted by the application",
    ("signal.name", "signal.state"),
)

__all__ = [
    "AI_ANALYSIS_DURATION_SECONDS",
    "AI_CACHE_OPERATION_DURATION_SECONDS",
    "AI_CACHE_REQUESTS_TOTAL",
    "AI_COST_USD_TOTAL",
    "AI_DEGRADATIONS_TOTAL",
    "AI_TOKENS_TOTAL",
    "ALERT_NUMERIC_PARSE_FAILURE_TOTAL",
    "DATABASE_EVENTS_COUNT",
    "DB_SESSION_DURATION_SECONDS",
    "DB_SESSION_TOTAL",
    "DB_POOL_CHECKED_OUT",
    "DB_POOL_SIZE",
    "DEEP_ANALYSIS_TOTAL",
    "FORWARD_RETRY_TOTAL",
    "FORWARD_DELIVERY_DURATION_SECONDS",
    "FORWARD_DELIVERY_TOTAL",
    "FORWARD_OUTBOX_PROCESS_DURATION_SECONDS",
    "FORWARD_OUTBOX_RECORDS_TOTAL",
    "HTTP_SERVER_REQUEST_BODY_BYTES",
    "HTTP_SERVER_REQUEST_DURATION_SECONDS",
    "HTTP_SERVER_REQUESTS_TOTAL",
    "KNOWN_SOURCES",
    "OPENAI_ERRORS_TOTAL",
    "OBSERVABILITY_EVENTS_TOTAL",
    "OBSERVABILITY_SIGNAL_TOTAL",
    "QUEUE_OPERATION_DURATION_SECONDS",
    "QUEUE_OPERATIONS_TOTAL",
    "REDIS_OPERATION_DURATION_SECONDS",
    "REDIS_OPERATIONS_TOTAL",
    "SCHEDULED_TASK_DURATION_SECONDS",
    "SCHEDULED_TASK_LAG_SECONDS",
    "SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME",
    "SCHEDULED_TASK_RUNS_TOTAL",
    "SECURITY_CHECKS_TOTAL",
    "WEBHOOK_DEAD_LETTER_TOTAL",
    "WEBHOOK_INGRESS_PAYLOAD_BYTES",
    "WEBHOOK_MQ_GROUP_LAG",
    "WEBHOOK_MQ_GROUP_PENDING",
    "WEBHOOK_MQ_STREAM_LENGTH",
    "WEBHOOK_NOISE_REDUCED_TOTAL",
    "WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS",
    "WEBHOOK_NOISE_EVALUATIONS_TOTAL",
    "WEBHOOK_PIPELINE_STEP_DURATION_SECONDS",
    "WEBHOOK_PIPELINE_STEP_TOTAL",
    "WEBHOOK_PROCESSING_DURATION_SECONDS",
    "WEBHOOK_PROCESSING_STATUS_COUNT",
    "WEBHOOK_PROCESSING_STATUS_TOTAL",
    "WEBHOOK_RECEIVED_TOTAL",
    "WEBHOOK_RECOVERY_POLLED_TOTAL",
    "WEBHOOK_RUNNING_TASKS",
    "WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL",
    "WEBHOOK_STORM_SUPPRESSED_TOTAL",
    "WEBHOOK_STUCK_STATUS_COUNT",
    "WORKER_TASK_DURATION_SECONDS",
    "WORKER_TASKS_TOTAL",
    "Counter",
    "Gauge",
    "Histogram",
    "sanitize_source",
    "setup_metrics",
    "start_background_metrics_server",
    "update_db_pool_metrics",
]
