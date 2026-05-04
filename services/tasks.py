"""TaskIQ 异步任务定义

将原有的 Pollers 逻辑转化为 TaskIQ 任务，并支持统一的依赖注入。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends as Depends

from core.taskiq_broker import broker
from db.session import get_db_session
from services.data_maintenance import archive_old_data_by_policy
from services.forward_retry_poller import poll_pending_retries
from services.metrics_poller import refresh_all_metrics
from services.openclaw_poller import poll_pending_analyses
from services.recovery_poller import run_recovery_scan

logger = logging.getLogger("webhook_service.tasks")


@broker.task(task_name="maintenance_task")
async def run_maintenance_task():
    """每日数据维护与归档任务"""
    logger.info("[Tasks] 开始执行定时维护任务...")
    moved = await archive_old_data_by_policy()
    logger.info(f"[Tasks] 维护任务完成，移动了 {moved} 条记录")


@broker.task(task_name="recovery_task")
async def run_recovery_task():
    """僵尸事件恢复任务"""
    logger.debug("[Tasks] 执行僵尸事件恢复扫描...")
    await run_recovery_scan()


@broker.task(task_name="forward_retry_task")
async def run_forward_retry_task():
    """转发失败重试任务"""
    logger.debug("[Tasks] 执行转发重试扫描...")
    await poll_pending_retries()


@broker.task(task_name="openclaw_poll_task")
async def run_openclaw_poll_task():
    """OpenClaw 结果轮询任务"""
    logger.debug("[Tasks] 执行 OpenClaw 结果轮询...")
    await poll_pending_analyses()


@broker.task(task_name="metrics_refresh_task")
async def run_metrics_refresh_task():
    """刷新系统指标 (DB 状态, 队列堆积等)"""
    await refresh_all_metrics()


@broker.task(task_name="webhook_process_task")
async def process_webhook_task(
    event_id: int,
    client_ip: str | None = None,
    session: AsyncSession = Depends(get_db_session)  # noqa: B008
):
    """异步处理单条 Webhook 事件"""
    from services.pipeline import handle_webhook_process
    logger.info(f"[Tasks] 异步处理 Webhook 事件: ID={event_id}")
    await handle_webhook_process(event_id=event_id, client_ip=client_ip, session=session)
