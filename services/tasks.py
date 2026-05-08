"""TaskIQ 异步任务定义。

包括：
- webhook_process_task：消费 webhook 队列
- 定时轮询任务：由 TaskIQ Scheduler 触发入队，由 Worker 执行
"""

import logging
import time
from collections.abc import Awaitable

from core.config import Config
from core.metrics import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
)
from core.taskiq_broker import broker
from db.session import session_scope

logger = logging.getLogger("webhook_service.tasks")

_last_success_by_name: dict[str, float] = {}


async def _run_scheduled(name: str, interval_seconds: int, fn: Awaitable[object]) -> None:
    start = time.time()
    try:
        await fn
        SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status="success").inc()
        now = time.time()
        prev = _last_success_by_name.get(name)
        if prev is not None:
            lag = max(0.0, now - prev - float(interval_seconds))
            SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
        else:
            SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(0.0)
        _last_success_by_name[name] = now
        SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name=name).set(now)
    except Exception:
        SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status="error").inc()
        last = _last_success_by_name.get(name)
        if last is not None:
            lag = max(0.0, time.time() - last - float(interval_seconds))
            SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
        raise
    finally:
        SCHEDULED_TASK_DURATION_SECONDS.labels(name=name).observe(time.time() - start)


@broker.task(task_name="webhook_process_task")
async def process_webhook_task(
    event_id: int,
    client_ip: str | None = None,
) -> None:
    """异步处理单条 Webhook 事件"""
    from services.pipeline import handle_webhook_process

    logger.info(f"[Tasks] 异步处理 Webhook 事件: ID={event_id}")
    async with session_scope() as session:
        await handle_webhook_process(event_id=event_id, client_ip=client_ip or "", session=session)


@broker.task(
    task_name="scheduled_recovery_scan",
    schedule=[
        {"interval": Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, "schedule_id": "recovery_scan_interval_seconds"}
    ],
)
async def scheduled_recovery_scan() -> None:
    from services.recovery_poller import run_recovery_scan

    await _run_scheduled("recovery_scan", Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, run_recovery_scan())


@broker.task(
    task_name="scheduled_metrics_refresh",
    schedule=[
        {
            "interval": Config.server.METRICS_REFRESH_INTERVAL_SECONDS,
            "schedule_id": "metrics_refresh_interval_seconds",
        }
    ],
)
async def scheduled_metrics_refresh() -> None:
    from services.metrics_poller import refresh_all_metrics

    await _run_scheduled("metrics_refresh", Config.server.METRICS_REFRESH_INTERVAL_SECONDS, refresh_all_metrics())


@broker.task(
    task_name="scheduled_openclaw_poll",
    schedule=[
        {
            "interval": Config.server.OPENCLAW_POLL_INTERVAL_SECONDS,
            "schedule_id": "openclaw_poll_interval_seconds",
        }
    ],
)
async def scheduled_openclaw_poll() -> None:
    from services.openclaw_poller import poll_pending_analyses

    await _run_scheduled("openclaw_poll", Config.server.OPENCLAW_POLL_INTERVAL_SECONDS, poll_pending_analyses())


@broker.task(
    task_name="scheduled_forward_retry_poll",
    schedule=[
        {"interval": Config.retry.FORWARD_RETRY_POLL_INTERVAL, "schedule_id": "forward_retry_poll_interval_seconds"}
    ],
)
async def scheduled_forward_retry_poll() -> None:
    if not Config.retry.ENABLE_FORWARD_RETRY:
        return
    from services.forward_retry_poller import poll_pending_retries

    await _run_scheduled("forward_retry_poll", Config.retry.FORWARD_RETRY_POLL_INTERVAL, poll_pending_retries())


@broker.task(
    task_name="scheduled_data_maintenance", schedule=[{"cron": f"0 {Config.maintenance.MAINTENANCE_HOUR} * * *"}]
)
async def scheduled_data_maintenance() -> None:
    from services.data_maintenance import archive_old_data_by_policy

    start = time.time()
    try:
        await archive_old_data_by_policy()
        SCHEDULED_TASK_RUNS_TOTAL.labels(name="data_maintenance", status="success").inc()
        SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name="data_maintenance").set(time.time())
        SCHEDULED_TASK_LAG_SECONDS.labels(name="data_maintenance").set(0.0)
    except Exception:
        SCHEDULED_TASK_RUNS_TOTAL.labels(name="data_maintenance", status="error").inc()
        raise
    finally:
        SCHEDULED_TASK_DURATION_SECONDS.labels(name="data_maintenance").observe(time.time() - start)
