"""Redis Stream backlog health: depth vs MAXLEN, pending, consumer lag.

Surfaces the queue instruments the metrics poller already emits (to OTel) on
the operator dashboard itself. Read-only, best-effort: any unreadable metric
degrades to None rather than erroring the panel.

The trim-loss risk is keyed on the UNCONSUMED backlog (undelivered ``lag`` +
delivered-but-un-acked ``pending``), NOT on total stream length ``depth``: a
healthy busy stream sits permanently near ``MAXLEN`` because XACK does not
remove entries (they are reclaimed by the approximate trim), so ``depth`` is
retention, not backlog. Only un-acked entries are lost when the trim fires, so
``backlog`` / ``backlog_fraction`` drive the ``backlogged`` flag; ``depth`` and
``fill_fraction`` are reported for context only.
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
    # Unconsumed backlog = undelivered (lag) + delivered-un-acked (pending);
    # this is the set actually at risk when the stream trims, unlike total depth.
    backlog = None if (pending is None and lag is None) else (pending or 0) + (lag or 0)
    backlog_fraction = round(backlog / maxlen, 4) if (backlog is not None and maxlen > 0) else None
    return {
        "stream": queue,
        "depth": depth,
        "pending": pending,
        "lag": lag,
        "backlog": backlog,
        "maxlen": maxlen,
        "fill_fraction": fill_fraction,
        "backlog_fraction": backlog_fraction,
        "warn_fraction": warn_fraction,
        "high_water_fraction": high_water_fraction,
        # True only when the warn threshold is enabled AND the live UNCONSUMED
        # backlog crossed it — not total depth (a full-but-acked stream is fine).
        "backlogged": bool(warn_fraction > 0 and backlog_fraction is not None and backlog_fraction >= warn_fraction),
    }


__all__ = ["get_queue_health"]
