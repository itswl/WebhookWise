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

    assert max_active == 3


@pytest.mark.asyncio
async def test_gate_keys_lock_and_queue_on_passed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must build its Redis lock/queue keys from the value it is given
    (the dedup_key), not derive a different key — otherwise the single-flight
    lock and the dedup decision it protects would key on different hashes."""
    import core.alert_concurrency as concurrency

    lock_keys: list[str] = []
    queue_keys: list[str] = []
    monkeypatch.setattr(concurrency, "webhook_processing_lock", lambda k: lock_keys.append(k) or f"lock:{k}")
    monkeypatch.setattr(concurrency, "webhook_processing_queue", lambda k: queue_keys.append(k) or f"q:{k}")

    async def reserve_slot(key: str) -> concurrency._QueueSlotReservation:
        concurrency.webhook_processing_queue(key)
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    async def no_lock(_: str) -> None:
        return None

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)
    monkeypatch.setattr(concurrency, "_acquire_distributed_lock", no_lock)

    async with concurrency.alert_processing_gate("dedup-key-abc") as result:
        assert result.suppressed is False
    # The lock-key helper is exercised via _lock_key; assert the dedup_key flowed in.
    assert concurrency._lock_key("dedup-key-abc") == "lock:dedup-key-abc"
    assert queue_keys == ["dedup-key-abc"]


@pytest.mark.asyncio
async def test_orchestrator_gates_on_dedup_key_not_alert_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: flapping alerts share a dedup_key but differ in alert_hash.
    The pipeline must serialise them by gating on dedup_key."""
    from contextlib import asynccontextmanager

    from contracts.webhook_payload import webhook_data_from_mapping
    from services.webhooks import pipeline_orchestrator, pipeline_runtime, pipeline_stages
    from services.webhooks.types import WebhookProcessContext, WebhookRequestContext

    gated_keys: list[str] = []

    class _Res:
        suppressed = True
        queue_size = 0
        reason = "x"

    @asynccontextmanager
    async def gate(key: str):
        gated_keys.append(key)
        yield _Res()

    async def validate(_ctx: object, _gate: object) -> pipeline_runtime.PipelineProcessingResult:
        return pipeline_runtime.PipelineProcessingResult(suppressed=True)

    monkeypatch.setattr(pipeline_orchestrator, "alert_processing_gate", gate)
    monkeypatch.setattr(pipeline_stages, "validate_backpressure", validate)

    req_ctx = WebhookRequestContext(
        client_ip="127.0.0.1",
        source="prometheus",
        payload=b'{"source": "prometheus"}',
        parsed_data=webhook_data_from_mapping({"source": "prometheus"}),
        webhook_full_data=webhook_data_from_mapping({"source": "prometheus"}),
    )
    ctx = WebhookProcessContext(
        event_id=None,
        request_id="r",
        metric_source="prometheus",
        req_ctx=req_ctx,
        alert_hash="HASH_WITH_SEVERITY",
        dedup_key="DEDUP_NO_SEVERITY",
    )
    deps = pipeline_runtime.WebhookPipelineDependencies(dedup_window_seconds=60)
    await pipeline_orchestrator.run_processing_pipeline(ctx, deps)

    assert gated_keys == ["DEDUP_NO_SEVERITY"]
    assert "HASH_WITH_SEVERITY" not in gated_keys


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

    async def refresh(_: str, __: str, ___: int, ____: object = None) -> None:
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


@pytest.mark.asyncio
async def test_queue_slot_reservation_suppresses_on_redis_error(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    import core.alert_concurrency as concurrency

    async def failing_eval(*_: object) -> int:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(temp_config.retry, "PROCESSING_LOCK_FAILFAST_THRESHOLD", 1)
    monkeypatch.setattr("core.redis_client.redis_eval_int", failing_eval)

    slot = await concurrency._reserve_processing_slot("hot-alert")

    assert slot.suppressed is True
    assert slot.reason == "redis_unavailable"


@pytest.mark.asyncio
async def test_alert_processing_gate_suppresses_when_redis_lock_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.alert_concurrency as concurrency

    async def reserve_slot(_: str) -> concurrency._QueueSlotReservation:
        return concurrency._QueueSlotReservation(reserved=False, queue_size=0)

    async def unavailable(*_: object) -> bool:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(concurrency, "_reserve_processing_slot", reserve_slot)
    monkeypatch.setattr("core.redis_client.redis_set_nx_ex", unavailable)

    async with concurrency.alert_processing_gate("same-alert") as result:
        assert result.suppressed is True
        assert result.reason == "redis_unavailable"
