"""OpenTelemetry metric instruments grouped by component domain."""

from __future__ import annotations

import logging
import re
import threading

from core.observability.env import env_int
from core.observability.metrics_base import Counter, Gauge, Histogram, setup_meter_provider

SOURCE_LABEL_MAX_LENGTH = 50
_SOURCE_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9_.-]+")
_SOURCE_LABEL_LIMIT = env_int("WEBHOOKWISE_SOURCE_LABEL_LIMIT", 128)
_SOURCE_LABEL_LIMIT_FALLBACK = "other"
_seen_sources: set[str] = set()
_seen_sources_lock = threading.Lock()


def _enforce_source_limit(source: str) -> str:
    if source in {"unknown", _SOURCE_LABEL_LIMIT_FALLBACK}:
        return source
    if _SOURCE_LABEL_LIMIT <= 0:
        return _SOURCE_LABEL_LIMIT_FALLBACK
    with _seen_sources_lock:
        if source in _seen_sources:
            return source
        if len(_seen_sources) >= _SOURCE_LABEL_LIMIT:
            return _SOURCE_LABEL_LIMIT_FALLBACK
        _seen_sources.add(source)
    return source


def sanitize_source(source: str) -> str:
    if not source:
        return "unknown"
    normalized = _SOURCE_LABEL_INVALID_CHARS.sub("-", str(source).lower().strip())
    normalized = normalized.strip("._-")
    if not normalized:
        return "unknown"
    return _enforce_source_limit(normalized[:SOURCE_LABEL_MAX_LENGTH])


def _reset_source_label_cache_for_tests() -> None:
    with _seen_sources_lock:
        _seen_sources.clear()


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
OPENAI_ERRORS_TOTAL = Counter("ai.request.errors", "AI provider call errors total", ("error.type",))
DEEP_ANALYSIS_TOTAL = Counter("ai.deep_analysis", "Deep analysis task result count", ("webhook.status", "ai.engine"))


DATABASE_EVENTS_COUNT = Gauge("webhook.events.count", "Current number of webhook events in active table")
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
FORWARD_RULE_MATCH_TOTAL = Counter(
    "forward.rule.matches",
    "Forward rule match count",
    ("forward.rule_name", "forward.target_type"),
)
FORWARD_OUTBOX_PROCESS_DURATION_SECONDS = Histogram(
    "forward.outbox.process.duration",
    "Forwarding outbox processing duration",
    ("forward.target_type", "forward.status"),
    unit="s",
)
FORWARD_OUTBOX_BACKLOG_AGE_SECONDS = Gauge(
    "forward.outbox.backlog.age",
    "Age of the oldest active forwarding outbox record",
    ("forward.target_type", "forward.status"),
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
REDIS_HEALTH_STATE = Gauge(
    "redis.health.state",
    "Current Redis health state as 1 for active and 0 for inactive states",
    ("redis.state",),
)
REDIS_UNAVAILABLE_TOTAL = Counter(
    "redis.unavailable",
    "Redis unavailable degradations by component/action",
    ("redis.component", "redis.action"),
)


CIRCUIT_BREAKER_REQUESTS_TOTAL = Counter(
    "circuit_breaker.requests",
    "Circuit breaker request decisions and outcomes",
    ("circuit_breaker.name", "circuit_breaker.outcome"),
)

CIRCUIT_BREAKER_TRANSITIONS_TOTAL = Counter(
    "circuit_breaker.transitions",
    "Circuit breaker state transitions",
    ("circuit_breaker.name", "circuit_breaker.state"),
)

CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker.state",
    "Current circuit breaker state as 1 for the active state and 0 for inactive states",
    ("circuit_breaker.name", "circuit_breaker.state"),
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


SECURITY_CHECKS_TOTAL = Counter(
    "security.checks",
    "Security check decision count",
    ("security.check", "security.result"),
)


WEBHOOK_RECEIVED_TOTAL = Counter(
    "webhook.received",
    "Total number of webhooks received",
    ("webhook.source", "webhook.status"),
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
WEBHOOK_PROCESSING_DURATION_SECONDS = Histogram(
    "webhook.processing.duration",
    "Time spent from start of pipeline to finish",
    ("webhook.source", "webhook.outcome"),
    unit="s",
)
WEBHOOK_ANALYSIS_ROUTE_TOTAL = Counter(
    "webhook.analysis.route",
    "Webhook analysis route decisions",
    ("webhook.source", "webhook.route"),
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
ALERT_NUMERIC_PARSE_FAILURE_TOTAL = Counter(
    "webhook.parse.failures",
    "Alert numeric field parse failures during rule analysis",
    ("webhook.source", "webhook.field", "error.reason"),
)
WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL = Counter(
    "webhook.semaphore.timeouts",
    "Semaphore acquisition timeout count",
)
WEBHOOK_STORM_SUPPRESSED_TOTAL = Counter(
    "webhook.storm.suppressed",
    "Webhook storm fail-fast suppression count",
    ("webhook.source",),
)
WEBHOOK_RUNNING_TASKS = Gauge("webhook.running_tasks", "Currently running webhook processing tasks")
WEBHOOK_DEAD_LETTER_TOTAL = Counter("webhook.dead_letter", "Non-retryable dead letter event count")
WEBHOOK_PROCESSING_STATUS_COUNT = Gauge(
    "webhook.processing.status_count",
    "Webhook event count by processing status",
    ("webhook.status",),
)
WEBHOOK_IDENTITY_DEGRADED_TOTAL = Counter(
    "webhook.identity.degraded",
    "Webhook identity degraded count (no adapter-produced alert identity)",
    ("webhook.source",),
)


def setup_metrics(app: object | None = None) -> None:
    setup_meter_provider()
    update_db_pool_metrics()


def update_db_pool_metrics() -> None:
    try:
        from db.engine import get_db_pool_capacity, get_db_pool_checked_out, get_engine

        engine = get_engine()
        if engine is None:
            return
        DB_POOL_SIZE.set_callback(lambda: get_db_pool_capacity(engine))
        DB_POOL_CHECKED_OUT.set_callback(lambda: get_db_pool_checked_out(engine))
    except (AttributeError, RuntimeError) as e:
        logging.getLogger("webhook_service").warning("[Metrics] unable to refresh DB pool metrics: %s", e)


__all__ = [
    "AI_ANALYSIS_DURATION_SECONDS",
    "AI_CACHE_OPERATION_DURATION_SECONDS",
    "AI_CACHE_REQUESTS_TOTAL",
    "AI_COST_USD_TOTAL",
    "AI_DEGRADATIONS_TOTAL",
    "AI_TOKENS_TOTAL",
    "ALERT_NUMERIC_PARSE_FAILURE_TOTAL",
    "Counter",
    "CIRCUIT_BREAKER_REQUESTS_TOTAL",
    "CIRCUIT_BREAKER_STATE",
    "CIRCUIT_BREAKER_TRANSITIONS_TOTAL",
    "DATABASE_EVENTS_COUNT",
    "DB_POOL_CHECKED_OUT",
    "DB_POOL_SIZE",
    "DB_SESSION_DURATION_SECONDS",
    "DB_SESSION_TOTAL",
    "DEEP_ANALYSIS_TOTAL",
    "FORWARD_DELIVERY_DURATION_SECONDS",
    "FORWARD_DELIVERY_TOTAL",
    "FORWARD_OUTBOX_BACKLOG_AGE_SECONDS",
    "FORWARD_OUTBOX_PROCESS_DURATION_SECONDS",
    "FORWARD_OUTBOX_RECORDS_TOTAL",
    "FORWARD_RULE_MATCH_TOTAL",
    "Gauge",
    "Histogram",
    "OBSERVABILITY_EVENTS_TOTAL",
    "OBSERVABILITY_SIGNAL_TOTAL",
    "OPENAI_ERRORS_TOTAL",
    "QUEUE_OPERATION_DURATION_SECONDS",
    "QUEUE_OPERATIONS_TOTAL",
    "REDIS_HEALTH_STATE",
    "REDIS_UNAVAILABLE_TOTAL",
    "REDIS_OPERATION_DURATION_SECONDS",
    "REDIS_OPERATIONS_TOTAL",
    "SCHEDULED_TASK_DURATION_SECONDS",
    "SCHEDULED_TASK_LAG_SECONDS",
    "SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME",
    "SCHEDULED_TASK_RUNS_TOTAL",
    "SECURITY_CHECKS_TOTAL",
    "WEBHOOK_DEAD_LETTER_TOTAL",
    "WEBHOOK_INGRESS_PAYLOAD_BYTES",
    "WEBHOOK_ANALYSIS_ROUTE_TOTAL",
    "WEBHOOK_IDENTITY_DEGRADED_TOTAL",
    "WEBHOOK_MQ_GROUP_LAG",
    "WEBHOOK_MQ_GROUP_PENDING",
    "WEBHOOK_MQ_STREAM_LENGTH",
    "WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS",
    "WEBHOOK_NOISE_EVALUATIONS_TOTAL",
    "WEBHOOK_NOISE_REDUCED_TOTAL",
    "WEBHOOK_PIPELINE_STEP_DURATION_SECONDS",
    "WEBHOOK_PIPELINE_STEP_TOTAL",
    "WEBHOOK_PROCESSING_DURATION_SECONDS",
    "WEBHOOK_PROCESSING_STATUS_COUNT",
    "WEBHOOK_PROCESSING_STATUS_TOTAL",
    "WEBHOOK_RECEIVED_TOTAL",
    "WEBHOOK_RUNNING_TASKS",
    "WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL",
    "WEBHOOK_STORM_SUPPRESSED_TOTAL",
    "WORKER_TASK_DURATION_SECONDS",
    "WORKER_TASKS_TOTAL",
    "sanitize_source",
    "setup_metrics",
    "update_db_pool_metrics",
]
