"""TaskIQ 异步任务定义 

将原有的 Pollers 逻辑转化为 TaskIQ 任务。
"""

import logging
from datetime import datetime

from core.taskiq_broker import broker
from services.data_maintenance import archive_old_data_by_policy
from services.recovery_poller import RecoveryPoller
from services.forward_retry_poller import poll_pending_retries
from services.openclaw_poller import poll_pending_analyses

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
    poller = RecoveryPoller()
    # 直接调用内部恢复逻辑，不再启动循环
    await poller._do_recover()


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


@broker.task(task_name="webhook_process_task")
async def process_webhook_task(event_id: int, client_ip: str | None = None):
    """异步处理单条 Webhook 事件"""
    from services.pipeline import handle_webhook_process
    logger.info(f"[Tasks] 异步处理 Webhook 事件: ID={event_id}")
    await handle_webhook_process(event_id=event_id, client_ip=client_ip)
