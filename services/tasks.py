"""TaskIQ 异步任务定义（仅保留队列消费任务）

所有定时轮询任务已迁移至 receiver 进程的 asyncio 循环（core/app.py）。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends as Depends

from core.taskiq_broker import broker
from db.session import get_db_session

logger = logging.getLogger("webhook_service.tasks")


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
