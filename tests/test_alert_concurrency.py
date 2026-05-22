import asyncio

import pytest


@pytest.mark.asyncio
async def test_alert_processing_gate_serializes_same_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.alert_concurrency as concurrency

    async def reserve_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)

    async def no_distributed_lock(_: str) -> None:
        return None

    monkeypatch.setattr(concurrency, "_acquire_distributed_lock", no_distributed_lock)

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
async def test_alert_processing_gate_releases_distributed_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.alert_concurrency as concurrency

    released: list[tuple[str, str]] = []

    async def reserve_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    async def acquire(_: str) -> tuple[str, str]:
        return "lock:key", "owner-token"

    async def release(key: str, token: str) -> None:
        released.append((key, token))

    async def refresh(_: str, __: str, ___: int) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)
    monkeypatch.setattr(concurrency, "_acquire_distributed_lock", acquire)
    monkeypatch.setattr(concurrency, "_release_distributed_lock", release)
    monkeypatch.setattr(concurrency, "_refresh_distributed_lock", refresh)

    async with concurrency.alert_processing_gate("same-alert") as result:
        assert result.suppressed is False

    assert released == [("lock:key", "owner-token")]


@pytest.mark.asyncio
async def test_alert_processing_gate_suppresses_when_distributed_lock_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.alert_concurrency as concurrency

    async def reserve_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    async def timeout(_: str) -> tuple[str, str]:
        return "", ""

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)
    monkeypatch.setattr(concurrency, "_acquire_distributed_lock", timeout)

    async with concurrency.alert_processing_gate("hot-alert") as result:
        assert result.suppressed is True
        assert result.reason == "alert_processing_lock_timeout"


@pytest.mark.asyncio
async def test_alert_processing_gate_suppresses_when_failfast_threshold_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.alert_concurrency as concurrency

    async def suppressed_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=3, suppressed=True)

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", suppressed_slot)

    async with concurrency.alert_processing_gate("hot-alert") as result:
        assert result.suppressed is True
        assert result.queue_size == 3


@pytest.mark.asyncio
async def test_alert_processing_gate_does_not_hold_local_lock_while_waiting_for_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.alert_concurrency as concurrency

    async def reserve_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    acquire_calls = 0
    both_waiting = asyncio.Event()
    release_waiters = asyncio.Event()

    async def slow_timeout(_: str) -> tuple[str, str]:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 2:
            both_waiting.set()
        await release_waiters.wait()
        return "", ""

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)
    monkeypatch.setattr(concurrency, "_acquire_distributed_lock", slow_timeout)

    async def enter_gate() -> None:
        async with concurrency.alert_processing_gate("same-alert"):
            pass

    tasks = [asyncio.create_task(enter_gate()), asyncio.create_task(enter_gate())]
    await asyncio.wait_for(both_waiting.wait(), timeout=0.2)
    release_waiters.set()
    await asyncio.gather(*tasks)

    assert acquire_calls == 2


@pytest.mark.asyncio
async def test_local_alert_lock_releases_ref_when_cancelled_while_waiting() -> None:
    import core.alert_concurrency as concurrency

    concurrency._lock_refs.clear()
    blocker = await concurrency._get_lock_ref("same-alert")
    await blocker.lock.acquire()

    task = asyncio.create_task(_wait_for_local_lock(concurrency, "same-alert"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    blocker.lock.release()
    await concurrency._release_lock_ref("same-alert", blocker)
    assert concurrency._lock_refs == {}


async def _wait_for_local_lock(concurrency: object, alert_hash: str) -> None:
    async with concurrency._local_alert_lock(alert_hash):  # type: ignore[attr-defined]
        pass


@pytest.mark.asyncio
async def test_queue_slot_reservation_does_not_count_suppressed_requests(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    import core.alert_concurrency as concurrency

    counts: dict[str, int] = {}

    async def fake_eval(script: str, numkeys: int, *args: object) -> int:
        key = str(args[0])
        if "current >= threshold" in script:
            threshold = int(args[2])
            current = counts.get(key, 0)
            if current >= threshold:
                return -current
            counts[key] = current + 1
            return counts[key]
        if "decr" in script:
            current = counts.get(key, 0)
            counts[key] = max(0, current - 1)
            return counts[key]
        return 0

    monkeypatch.setattr(temp_config.retry, "PROCESSING_LOCK_FAILFAST_THRESHOLD", 1)
    monkeypatch.setattr("core.redis_client.redis_eval_int", fake_eval)

    first = await concurrency._reserve_processing_slot("hot-alert")
    second = await concurrency._reserve_processing_slot("hot-alert")

    assert first == concurrency._QueueSlotReservation(reserved=True, queue_size=1, suppressed=False)
    assert second == concurrency._QueueSlotReservation(reserved=False, queue_size=1, suppressed=True)
    assert counts["queue:webhook:hot-alert"] == 1

    await concurrency._release_processing_slot("hot-alert")
    assert counts["queue:webhook:hot-alert"] == 0
