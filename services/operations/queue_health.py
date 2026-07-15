"""Redis Stream backlog health: depth vs MAXLEN, pending, consumer lag.

Surfaces the queue instruments the metrics poller already emits (to OTel) on
the operator dashboard itself, plus the fill fraction against MAXLEN so an
operator can see a backlog approaching the silent-trim boundary before it
happens. Read-only, best-effort: any unreadable metric degrades to None rather
than erroring the panel.
"""

from __future__ import annotations

from typing import Any

from redis.exceptions import RedisError

from core.app_context import get_config_manager
from core.logger import get_logger
from core.redis_streams import redis_xinfo_group_lag, redis_xlen, redis_xpending_pending

logger = get_logger("operations.queue_health")

_PROBE_ERRORS = (RedisError, RuntimeError, TypeError, ValueError)


async def get_queue_health() -> dict[str, Any]:
    mq = get_config_manager().mq
    queue = str(mq.WEBHOOK_MQ_QUEUE)
    group = str(mq.WEBHOOK_MQ_CONSUMER_GROUP)
    maxlen = int(mq.WEBHOOK_MQ_STREAM_MAXLEN or 0)
    warn_fraction = float(mq.WEBHOOK_MQ_BACKLOG_WARN_FRACTION or 0.0)
    high_water_fraction = float(mq.WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION or 0.0)

    depth = pending = lag = None
    try:
        depth = await redis_xlen(queue)
        pending = await redis_xpending_pending(queue, group)
        lag = await redis_xinfo_group_lag(queue, group)
    except _PROBE_ERRORS as e:
        logger.debug("[QueueHealth] probe failed: %s", e)

    fill_fraction = round(depth / maxlen, 4) if (depth is not None and maxlen > 0) else None
    return {
        "stream": queue,
        "depth": depth,
        "pending": pending,
        "lag": lag,
        "maxlen": maxlen,
        "fill_fraction": fill_fraction,
        "warn_fraction": warn_fraction,
        "high_water_fraction": high_water_fraction,
        # True only when the warn threshold is enabled AND the live depth crossed it.
        "backlogged": bool(warn_fraction > 0 and fill_fraction is not None and fill_fraction >= warn_fraction),
    }


__all__ = ["get_queue_health"]
