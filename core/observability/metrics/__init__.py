"""OpenTelemetry metric instruments grouped by component domain."""

from __future__ import annotations

import logging

from core.observability.metrics._metrics_ai import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_CACHE_OPERATION_DURATION_SECONDS,
    AI_CACHE_REQUESTS_TOTAL,
    AI_COST_USD_TOTAL,
    AI_DEGRADATIONS_TOTAL,
    AI_TOKENS_TOTAL,
    DEEP_ANALYSIS_TOTAL,
    OPENAI_ERRORS_TOTAL,
)
from core.observability.metrics._metrics_db import (
    DATABASE_EVENTS_COUNT,
    DB_POOL_CHECKED_OUT,
    DB_POOL_SIZE,
    DB_SESSION_DURATION_SECONDS,
    DB_SESSION_TOTAL,
)
from core.observability.metrics._metrics_forward import (
    FORWARD_DELIVERY_DURATION_SECONDS,
    FORWARD_DELIVERY_TOTAL,
    FORWARD_OUTBOX_BACKLOG_AGE_SECONDS,
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from core.observability.metrics._metrics_observability import OBSERVABILITY_EVENTS_TOTAL, OBSERVABILITY_SIGNAL_TOTAL
from core.observability.metrics._metrics_queue import (
    QUEUE_OPERATION_DURATION_SECONDS,
    QUEUE_OPERATIONS_TOTAL,
    REDIS_HEALTH_STATE,
    REDIS_OPERATION_DURATION_SECONDS,
    REDIS_OPERATIONS_TOTAL,
    WEBHOOK_MQ_GROUP_LAG,
    WEBHOOK_MQ_GROUP_PENDING,
    WEBHOOK_MQ_STREAM_LENGTH,
)
from core.observability.metrics._metrics_resilience import (
    CIRCUIT_BREAKER_REQUESTS_TOTAL,
    CIRCUIT_BREAKER_STATE,
    CIRCUIT_BREAKER_TRANSITIONS_TOTAL,
)
from core.observability.metrics._metrics_schedule import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
    WORKER_TASK_DURATION_SECONDS,
    WORKER_TASKS_TOTAL,
)
from core.observability.metrics._metrics_security import SECURITY_CHECKS_TOTAL
from core.observability.metrics._metrics_webhook import (
    ALERT_NUMERIC_PARSE_FAILURE_TOTAL,
    WEBHOOK_DEAD_LETTER_TOTAL,
    WEBHOOK_INGRESS_PAYLOAD_BYTES,
    WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS,
    WEBHOOK_NOISE_EVALUATIONS_TOTAL,
    WEBHOOK_NOISE_REDUCED_TOTAL,
    WEBHOOK_PIPELINE_STEP_DURATION_SECONDS,
    WEBHOOK_PIPELINE_STEP_TOTAL,
    WEBHOOK_PROCESSING_DURATION_SECONDS,
    WEBHOOK_PROCESSING_STATUS_COUNT,
    WEBHOOK_PROCESSING_STATUS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL,
    WEBHOOK_STORM_SUPPRESSED_TOTAL,
)
from core.observability.metrics.base import Counter, Gauge, Histogram, setup_meter_provider
from core.observability.metrics.source import sanitize_source


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
    "Gauge",
    "Histogram",
    "OBSERVABILITY_EVENTS_TOTAL",
    "OBSERVABILITY_SIGNAL_TOTAL",
    "OPENAI_ERRORS_TOTAL",
    "QUEUE_OPERATION_DURATION_SECONDS",
    "QUEUE_OPERATIONS_TOTAL",
    "REDIS_HEALTH_STATE",
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
