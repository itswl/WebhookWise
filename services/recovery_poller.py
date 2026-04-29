"""僵尸事件恢复 — 捞取停滞的 webhook 事件并重新处理，实现 At-Least-Once Delivery"""

import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from core.redis_client import get_redis
from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger(__name__)

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50

# 超过此时间仍处于 received/analyzing 的事件视为僵尸
_ZOMBIE_THRESHOLD_MINUTES = 5

_LOCK_KEY = "recovery:poller:lock"
_LOCK_TTL_SECONDS = 120  # 与调度间隔匹配

_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def recover_zombie_events() -> None:
    """捐取停滞的 webhook 事件并重新处理

    使用 Redis NX 分布式锁确保多 Worker 下仅一个实例执行。
    """
    redis = get_redis()
    lock_value = str(uuid.uuid4())
    if not await redis.set(_LOCK_KEY, lock_value, nx=True, ex=_LOCK_TTL_SECONDS):
        logger.debug("[Recovery] 另一个 worker 正在执行，跳过本轮")
        return

    try:
        await _do_recover()
    finally:
        with contextlib.suppress(Exception):
            await redis.eval(_RELEASE_LOCK_LUA, 1, _LOCK_KEY, lock_value)


async def _do_recover() -> None:
    """实际恢复逻辑"""
    threshold = datetime.now() - timedelta(minutes=_ZOMBIE_THRESHOLD_MINUTES)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.processing_status.in_(["received", "analyzing"]))
            .where(WebhookEvent.created_at < threshold)
            .limit(_MAX_RECOVER_BATCH)
        )
        zombie_events = result.scalars().all()

    if not zombie_events:
        return

    logger.info(f"[Recovery] 发现 {len(zombie_events)} 条僵尸事件，开始恢复处理")

    for event in zombie_events:
        try:
            await _reprocess_event(event)
        except Exception:  # noqa: PERF203
            logger.exception(f"[Recovery] 恢复事件 {event.id} 失败")


async def _reprocess_event(event: WebhookEvent) -> None:
    """重新处理单条僵尸事件"""
    from services.pipeline import handle_webhook_process

    source = event.source or "unknown"
    logger.info(f"[Recovery] 重新处理事件 id={event.id}, source={source}")

    await handle_webhook_process(
        event_id=event.id,
        client_ip="recovery",
    )
