import logging
import uuid
from datetime import datetime

from core.distributed_lock import DistributedLock
from services.data_maintenance import archive_old_data

logger = logging.getLogger("webhook_service.maintenance")

# 记录上次执行日期，避免同一天重复执行
_last_run_date = None

_LOCK_KEY = "maintenance:poller:lock"
_LOCK_TTL_SECONDS = 600  # 与调度间隔匹配


async def check_and_run_maintenance():
    """检查是否到达凌晨 3 点，是则执行每日维护任务（归档旧数据）。

    由 poller_scheduler 每 600s 调度一次。
    使用 Redis NX 分布式锁确保多 Worker 下仅一个实例执行。
    """
    global _last_run_date

    now = datetime.now()
    if now.hour != 3 or now.date() == _last_run_date:
        return

    lock = DistributedLock(key=_LOCK_KEY, ttl=_LOCK_TTL_SECONDS, lock_value=str(uuid.uuid4()))
    async with lock as acquired:
        if not acquired:
            logger.debug("[Maintenance] 另一个 worker 正在执行，跳过本轮")
            return
        logger.info(f"[Maintenance] 开始执行凌晨维护任务 (当前时间: {now.strftime('%H:%M:%S')})")

        moved = await archive_old_data(archive_days=30)
        logger.info(f"[Maintenance] 归档任务完成，移动了 {moved} 条记录")

        _last_run_date = now.date()
