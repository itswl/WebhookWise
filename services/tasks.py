"""TaskIQ 异步任务定义。

包括：
- webhook_process_task：消费 webhook 队列
- 定时轮询任务：由 TaskIQ Scheduler 触发入队，由 Worker 执行
"""

import logging

from core.config import Config
from core.taskiq_broker import broker
from db.session import session_scope

logger = logging.getLogger("webhook_service.tasks")


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


@broker.task(task_name="scheduled_recovery_scan", schedule=[{"cron": "*/1 * * * *"}])
async def scheduled_recovery_scan() -> None:
    from services.recovery_poller import run_recovery_scan

    await run_recovery_scan()


@broker.task(task_name="scheduled_metrics_refresh", schedule=[{"cron": "*/1 * * * *"}])
async def scheduled_metrics_refresh() -> None:
    from services.metrics_poller import refresh_all_metrics

    await refresh_all_metrics()


@broker.task(task_name="scheduled_openclaw_poll", schedule=[{"cron": "*/1 * * * *"}])
async def scheduled_openclaw_poll() -> None:
    from services.openclaw_poller import poll_pending_analyses

    await poll_pending_analyses()


@broker.task(task_name="scheduled_forward_retry_poll", schedule=[{"cron": "*/1 * * * *"}])
async def scheduled_forward_retry_poll() -> None:
    if not Config.retry.ENABLE_FORWARD_RETRY:
        return
    from services.forward_retry_poller import poll_pending_retries

    await poll_pending_retries()


@broker.task(
    task_name="scheduled_data_maintenance", schedule=[{"cron": f"0 {Config.maintenance.MAINTENANCE_HOUR} * * *"}]
)
async def scheduled_data_maintenance() -> None:
    from services.data_maintenance import archive_old_data_by_policy

    await archive_old_data_by_policy()
