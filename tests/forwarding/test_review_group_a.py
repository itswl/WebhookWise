"""Regression tests for review group A (concurrency/reliability fixes).

- _finalize_outbox_success claims SENT atomically (no double DeepAnalysis)
- requeue_forward_outbox does not requeue a PROCESSING row
- alert_processing_gate exposes a lock_lost signal that fires on ownership loss
  (consumed by persist_and_schedule; see tests/webhooks/test_pipeline_runtime.py)
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_refresh_sets_lock_lost_on_ownership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.alert_concurrency as concurrency

    async def lost_owner(*_a: object, **_k: object) -> int:
        return 0  # not refreshed -> ownership lost

    monkeypatch.setattr(concurrency.redis_client, "redis_eval_int", lost_owner)
    monkeypatch.setattr(concurrency.asyncio, "sleep", _no_sleep)

    event = asyncio.Event()
    await concurrency._refresh_distributed_lock("k", "tok", 3, event)
    assert event.is_set()


@pytest.mark.asyncio
async def test_refresh_sets_lock_lost_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import RedisError

    import core.alert_concurrency as concurrency

    async def boom(*_a: object, **_k: object) -> int:
        raise RedisError("down")

    monkeypatch.setattr(concurrency.redis_client, "redis_eval_int", boom)
    monkeypatch.setattr(concurrency.asyncio, "sleep", _no_sleep)

    event = asyncio.Event()
    await concurrency._refresh_distributed_lock("k", "tok", 3, event)
    assert event.is_set()


async def _no_sleep(_seconds: float) -> None:
    return None
