from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from tests.helpers.metric_helpers import MetricCall, StubMetric


@dataclass
class _Policy:
    worker_id: str = "worker-a"
    background_scan_interval_seconds: int = 11
    metrics_refresh_interval_seconds: int = 17
    maintenance_hour: int = 4


@pytest.fixture
def task_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[MetricCall]]:
    from services.operations import tasks

    metric_calls: list[MetricCall] = []
    for attr in (
        "SCHEDULED_TASK_DURATION_SECONDS",
        "SCHEDULED_TASK_LAG_SECONDS",
        "SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME",
        "SCHEDULED_TASK_RUNS_TOTAL",
        "WEBHOOK_RUNNING_TASKS",
        "WORKER_TASK_DURATION_SECONDS",
        "WORKER_TASKS_TOTAL",
    ):
        monkeypatch.setattr(tasks, attr, StubMetric(metric_calls, attr))
    monkeypatch.setattr(tasks.TaskRuntimePolicy, "from_config", staticmethod(lambda: _Policy()))
    return tasks, metric_calls


def test_task_runtime_policy_helpers_use_current_config(task_runtime: tuple[Any, list[object]]) -> None:
    tasks, _metric_calls = task_runtime

    assert tasks._background_scan_interval_seconds() == 11
    assert tasks._metrics_refresh_interval_seconds() == 17
    assert tasks._maintenance_cron() == "0 4 * * *"


@pytest.mark.asyncio
async def test_scheduled_task_leader_acquires_and_releases_redis_lock(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[object]],
) -> None:
    tasks, _metric_calls = task_runtime
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def ensure_redis_available(reason: str) -> bool:
        calls.append(("health", (reason,)))
        return True

    async def redis_set_nx_ex(key: str, token: str, ttl: int) -> bool:
        calls.append(("setnx", (key, token.split(":", 1)[0], ttl)))
        return True

    async def redis_eval_int(script: str, numkeys: int, key: str, token: str) -> int:
        calls.append(("release", (script, numkeys, key, token.split(":", 1)[0])))
        return 1

    monkeypatch.setattr(tasks.redis_health, "ensure_redis_available", ensure_redis_available)
    monkeypatch.setattr(tasks.redis_client, "redis_set_nx_ex", redis_set_nx_ex)
    monkeypatch.setattr(tasks.redis_client, "redis_eval_int", redis_eval_int)
    monkeypatch.setattr(tasks, "_RELEASE_IF_OWNER_LUA", "release-if-owner")

    async with tasks._scheduled_task_leader("scan", 15, policy=_Policy(worker_id="worker-b")) as is_leader:
        assert is_leader is True

    assert calls[0] == ("health", ("scheduled_task:scan:leader",))
    assert calls[1][0] == "setnx"
    assert calls[1][1][0].endswith(":scan")
    assert calls[1][1][1:] == ("worker-b", 30)
    assert calls[2][0] == "release"


@pytest.mark.asyncio
async def test_scheduled_task_leader_skips_when_redis_or_lock_fails(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[object]],
) -> None:
    tasks, _metric_calls = task_runtime
    failures: list[str] = []

    async def redis_unavailable(_reason: str) -> bool:
        return False

    monkeypatch.setattr(tasks.redis_health, "ensure_redis_available", redis_unavailable)
    async with tasks._scheduled_task_leader("metrics", 10) as is_leader:
        assert is_leader is False

    async def redis_available(_reason: str) -> bool:
        return True

    async def lock_error(*_args: object) -> bool:
        raise RuntimeError("redis down")

    monkeypatch.setattr(tasks.redis_health, "ensure_redis_available", redis_available)
    monkeypatch.setattr(tasks.redis_health, "mark_redis_failure", lambda reason, _err: failures.append(reason))
    monkeypatch.setattr(tasks.redis_client, "redis_set_nx_ex", lock_error)

    async with tasks._scheduled_task_leader("metrics", 10) as is_leader:
        assert is_leader is False
    assert failures == ["scheduled_task:metrics:leader"]


@pytest.mark.asyncio
async def test_run_scheduled_closes_non_leader_coroutine(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[object]],
) -> None:
    tasks, _metric_calls = task_runtime
    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    @asynccontextmanager
    async def not_leader(_name: str, _interval: int) -> AsyncIterator[bool]:
        yield False

    monkeypatch.setattr(tasks, "_scheduled_task_leader", not_leader)

    await tasks._run_scheduled("scan", 10, work())

    assert ran is False


@pytest.mark.asyncio
async def test_run_scheduled_locked_records_success_lag_and_error_metrics(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    tasks, metric_calls = task_runtime
    spans: list[Any] = []
    logs: list[tuple[str, str, tuple[object, ...]]] = []

    class Span:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    @contextmanager
    def fake_span(_name: str, _attrs: dict[str, object]) -> Any:
        span = Span()
        spans.append(span)
        yield span

    async def ok() -> None:
        return None

    async def fail() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "otel_span", fake_span)
    monkeypatch.setattr(tasks.logger, "debug", lambda message, *args: logs.append(("debug", message, args)))
    monkeypatch.setattr(tasks.logger, "info", lambda message, *args: logs.append(("info", message, args)))
    monkeypatch.setattr(tasks.logger, "exception", lambda message, *args: logs.append(("exception", message, args)))
    runtime = tasks._ScheduledTaskRuntime()

    await tasks._run_scheduled_locked("metrics", 10, ok(), runtime=runtime)
    await tasks._run_scheduled_locked("metrics", 10, ok(), runtime=runtime)
    with pytest.raises(RuntimeError, match="boom"):
        await tasks._run_scheduled_locked("metrics", 10, fail(), runtime=runtime)

    assert [span.attributes["scheduler.task.status"] for span in spans] == ["success", "success", "error"]
    assert any(
        call[0] == "SCHEDULED_TASK_RUNS_TOTAL" and call[2] == {"name": "metrics", "status": "success"}
        for call in metric_calls
    )
    assert any(
        call[0] == "SCHEDULED_TASK_RUNS_TOTAL" and call[2] == {"name": "metrics", "status": "error"}
        for call in metric_calls
    )
    assert any(call[0] == "SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME" and call[3] == "set" for call in metric_calls)
    assert any(level == "debug" and "周期任务开始" in message for level, message, _args in logs)
    assert any(level == "debug" and "周期任务成功" in message for level, message, _args in logs)
    assert any(level == "exception" and "周期任务失败" in message for level, message, _args in logs)


def test_webhook_task_context_start_fallback_and_finish_emit_events(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    tasks, metric_calls = task_runtime
    events: list[tuple[str, dict[str, object]]] = []
    signals: list[tuple[str, str, dict[str, object]]] = []
    log_context: list[tuple[str, dict[str, object]]] = []
    fallback_tokens: list[str] = []

    monkeypatch.setattr(tasks, "generate_trace_id", lambda: "d" * 32)
    monkeypatch.setattr(tasks, "clear_log_context", lambda: log_context.append(("clear", {})))
    monkeypatch.setattr(tasks, "set_log_context", lambda **kwargs: log_context.append(("set", kwargs)))
    monkeypatch.setattr(tasks, "emit_event", lambda name, attrs: events.append((name, attrs)))
    monkeypatch.setattr(tasks, "record_signal", lambda name, state, attrs: signals.append((name, state, attrs)))
    monkeypatch.setattr(tasks, "set_fallback_trace_id", lambda trace_id: fallback_tokens.append(trace_id) or "token")
    monkeypatch.setattr(tasks, "reset_fallback_trace_id", lambda token: fallback_tokens.append(f"reset:{token}"))

    ctx = tasks._build_webhook_task_context(
        client_ip=None,
        source_name="",
        raw_headers=None,
        raw_body=None,
        request_id="req-42",
        received_at="2026-05-27T00:00:00Z",
        ingest_retry_count=2,
        traceparent=None,
    )

    assert ctx.source == "unknown"
    assert ctx.raw_headers == {}
    assert ctx.raw_body == ""
    assert ctx.traceparent.startswith(f"00-{'d' * 32}-")
    assert ctx.trace_headers["X-Request-Id"] == "req-42"

    tasks._start_webhook_task(ctx)
    token = tasks._set_webhook_task_fallback_trace(ctx)
    tasks._reset_webhook_task_fallback_trace(token)
    task_start = time.perf_counter() - 0.05
    tasks._finish_webhook_task(ctx, "completed", task_start)

    assert log_context[0] == ("clear", {})
    assert log_context[1] == ("set", {"request_id": "req-42", "webhook_source": "unknown"})
    assert fallback_tokens == ["d" * 32, "reset:token"]
    assert [event[0] for event in events] == ["webhook.task.started", "webhook.task.finished"]
    assert signals[0][0:2] == ("webhook.task", "completed")
    assert any(
        call[0] == "WORKER_TASKS_TOTAL" and call[1] == ("webhook_process_task", "completed") for call in metric_calls
    )


def test_reset_webhook_task_fallback_trace_tolerates_foreign_token(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[object]],
) -> None:
    tasks, _metric_calls = task_runtime
    monkeypatch.setattr(tasks, "reset_fallback_trace_id", lambda _token: (_ for _ in ()).throw(ValueError("foreign")))

    tasks._reset_webhook_task_fallback_trace("token")


@pytest.mark.asyncio
async def test_run_forward_outbox_task_records_success_and_error_metrics(
    monkeypatch: pytest.MonkeyPatch,
    task_runtime: tuple[Any, list[tuple[str, tuple[object, ...], dict[str, object], str, object]]],
) -> None:
    tasks, metric_calls = task_runtime
    from services.forwarding import outbox

    processed: list[int] = []

    async def process_success(outbox_id: int) -> None:
        processed.append(outbox_id)

    async def process_failure(_outbox_id: int) -> None:
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(outbox, "process_forward_outbox_by_id", process_success)
    await tasks.run_forward_outbox_task(101)
    assert processed == [101]

    monkeypatch.setattr(outbox, "process_forward_outbox_by_id", process_failure)
    with pytest.raises(RuntimeError, match="delivery failed"):
        await tasks.run_forward_outbox_task(102)

    assert any(
        call[0] == "WORKER_TASKS_TOTAL" and call[1] == ("forward_outbox_task", "success") for call in metric_calls
    )
    assert any(call[0] == "WORKER_TASKS_TOTAL" and call[1] == ("forward_outbox_task", "error") for call in metric_calls)
