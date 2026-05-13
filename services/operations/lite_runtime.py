"""In-process background loops for RUN_MODE=lite.

Lite mode keeps the HTTP API, webhook processing, and maintenance fallbacks in
one process. It deliberately avoids Redis/TaskIQ so small deployments can start
with only the API container plus PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from services.operations.policies import TaskRuntimePolicy

logger = logging.getLogger("webhook_service.lite_runtime")

_LiteJob = Callable[[], Awaitable[object]]


async def _run_periodic(name: str, interval_seconds: int, job: _LiteJob, stop_event: asyncio.Event) -> None:
    interval = max(5, int(interval_seconds))
    logger.info("[Lite] 后台任务启动 name=%s interval=%ss", name, interval)
    while not stop_event.is_set():
        try:
            await job()
        except Exception:
            logger.exception("[Lite] 后台任务失败 name=%s", name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
    logger.info("[Lite] 后台任务停止 name=%s", name)


async def _run_daily(name: str, job: _LiteJob, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=24 * 3600)
        return
    except TimeoutError:
        pass
    await _run_periodic(name, 24 * 3600, job, stop_event)


def start_lite_runtime(policy: TaskRuntimePolicy | None = None) -> tuple[asyncio.Event, list[asyncio.Task[None]]]:
    runtime_policy = policy or TaskRuntimePolicy.from_config()
    stop_event = asyncio.Event()

    async def outbox_scan() -> int:
        from services.forwarding.outbox import run_forward_outbox_scan

        return await run_forward_outbox_scan()

    async def openclaw_poll_scan() -> int:
        from services.analysis.openclaw_poller import run_openclaw_poll_scan

        return await run_openclaw_poll_scan()

    async def failed_forward_scan() -> int:
        from services.forwarding.retry import run_failed_forward_scan

        return await run_failed_forward_scan()

    async def data_maintenance() -> int:
        from services.operations.data_maintenance import archive_old_data_by_policy

        return await archive_old_data_by_policy()

    tasks = [
        asyncio.create_task(
            _run_periodic(
                "forward_outbox_scan",
                runtime_policy.recovery_scan_interval_seconds,
                outbox_scan,
                stop_event,
            )
        ),
        asyncio.create_task(
            _run_periodic(
                "openclaw_poll_scan",
                runtime_policy.recovery_scan_interval_seconds,
                openclaw_poll_scan,
                stop_event,
            )
        ),
        asyncio.create_task(
            _run_periodic(
                "failed_forward_scan",
                runtime_policy.recovery_scan_interval_seconds,
                failed_forward_scan,
                stop_event,
            )
        ),
        asyncio.create_task(_run_daily("data_maintenance", data_maintenance, stop_event)),
    ]
    return stop_event, tasks


async def stop_lite_runtime(stop_event: asyncio.Event, tasks: list[asyncio.Task[None]]) -> None:
    stop_event.set()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
