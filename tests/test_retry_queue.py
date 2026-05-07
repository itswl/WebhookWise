from unittest.mock import AsyncMock, patch

import pytest

from services.retry_queue import (
    FORWARD_RETRY_ZSET,
    WEBHOOK_RETRY_ZSET,
    compute_backoff_delay,
    drain_due_forward_retries,
    drain_due_webhook_retries,
    enqueue_forward_retry,
    enqueue_webhook_retry,
)


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


@pytest.mark.asyncio
async def test_enqueue_retry_ids_use_expected_zsets() -> None:
    redis = AsyncMock()

    with patch("services.retry_queue.get_redis", return_value=redis), patch(
        "services.retry_queue.time.time", return_value=1000
    ):
        await enqueue_webhook_retry(123, 30)
        await enqueue_forward_retry(456, 60)

    redis.zadd.assert_any_await(WEBHOOK_RETRY_ZSET, {"123": 1030})
    redis.zadd.assert_any_await(FORWARD_RETRY_ZSET, {"456": 1060})


@pytest.mark.asyncio
async def test_drain_due_retry_ids_filters_invalid_members() -> None:
    async def fake_eval(script: str, numkeys: int, zset: str, now: float, limit: int) -> str:
        assert numkeys == 1
        assert limit == 10
        return "1,bad,2"

    with patch("services.retry_queue.redis_eval_str", side_effect=fake_eval):
        assert await drain_due_webhook_retries(limit=10) == [1, 2]
        assert await drain_due_forward_retries(limit=10) == [1, 2]
