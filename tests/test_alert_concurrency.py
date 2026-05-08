import asyncio

import pytest


@pytest.mark.asyncio
async def test_alert_processing_gate_serializes_same_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.alert_concurrency as concurrency

    async def no_recent_queue(_: str) -> int:
        return 0

    monkeypatch.setattr(concurrency, "_count_recent_queue_size", no_recent_queue)

    active = 0
    max_active = 0

    async def enter_gate() -> None:
        nonlocal active, max_active
        async with concurrency.alert_processing_gate("same-alert"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(enter_gate(), enter_gate(), enter_gate())

    assert max_active == 1


@pytest.mark.asyncio
async def test_alert_processing_gate_suppresses_when_failfast_threshold_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.alert_concurrency as concurrency
    from core.config import Config

    monkeypatch.setattr(Config.retry, "PROCESSING_LOCK_FAILFAST_THRESHOLD", 2)

    async def high_recent_queue(_: str) -> int:
        return 3

    monkeypatch.setattr(concurrency, "_count_recent_queue_size", high_recent_queue)

    async with concurrency.alert_processing_gate("hot-alert") as result:
        assert result.suppressed is True
        assert result.queue_size == 3
