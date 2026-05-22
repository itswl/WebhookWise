"""Queue and Redis metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge, Histogram

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
