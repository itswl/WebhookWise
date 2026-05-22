from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def record_redis_operation(operation: str, awaitable: Awaitable[T]) -> T:
    from core.observability.metrics import REDIS_OPERATION_DURATION_SECONDS, REDIS_OPERATIONS_TOTAL
    from core.observability.tracing import span as otel_span
    from core.redis_health import mark_redis_failure, mark_redis_success

    start = time.perf_counter()
    status = "success"
    try:
        with otel_span(
            "redis.operation",
            {"db.system": "redis", "db.operation": operation, "redis.operation": operation},
        ):
            result = await awaitable
    except Exception as e:
        status = "error"
        mark_redis_failure(operation, e)
        raise
    else:
        mark_redis_success(operation)
        return result
    finally:
        REDIS_OPERATIONS_TOTAL.labels(operation, status).inc()
        REDIS_OPERATION_DURATION_SECONDS.labels(operation, status).observe(time.perf_counter() - start)
