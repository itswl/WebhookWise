"""Redis Stream 消费者 — 从 webhook:queue 拉取事件并调用 pipeline 处理。

使用 XREADGROUP + Consumer Group 实现可靠消费：
- 成功处理后 XACK 确认
- 失败不 ACK，留在 PEL 供 RecoveryPoller 补偿
"""

import asyncio
import os
import socket

from core.config import Config
from core.logger import logger
from core.redis_client import get_redis

_QUEUE: str = ""
_GROUP: str = ""


def _get_consumer_id() -> str:
    """生成 Consumer ID：优先使用 Config.server.WORKER_ID，否则 hostname:pid"""
    worker_id = Config.server.WORKER_ID
    if worker_id:
        return worker_id
    return f"{socket.gethostname()}:{os.getpid()}"


async def _init_consumer_group() -> None:
    """幂等创建 Consumer Group（若已存在则忽略 BUSYGROUP 异常）"""
    redis = get_redis()
    try:
        await redis.xgroup_create(name=_QUEUE, groupname=_GROUP, id="0", mkstream=True)
        logger.info(f"[MQ] Consumer Group 已创建: stream={_QUEUE}, group={_GROUP}")
    except Exception as e:
        # redis-py 对 BUSYGROUP 抛出 ResponseError
        if "BUSYGROUP" in str(e):
            logger.debug(f"[MQ] Consumer Group 已存在: {_GROUP}")
        else:
            raise


async def consume_webhook_queue(stop_event: asyncio.Event) -> None:
    """主消费循环：阻塞式拉取 Redis Stream 消息并处理。

    Args:
        stop_event: 优雅停机信号，set() 后退出循环
    """
    global _QUEUE, _GROUP
    _QUEUE = Config.server.WEBHOOK_MQ_QUEUE
    _GROUP = Config.server.WEBHOOK_MQ_CONSUMER_GROUP
    batch_size = Config.server.WEBHOOK_MQ_CONSUMER_BATCH_SIZE
    block_ms = Config.server.WEBHOOK_MQ_CONSUMER_TIMEOUT_MS
    consumer_id = _get_consumer_id()

    await _init_consumer_group()

    # 延迟导入避免循环引用
    from services.pipeline import handle_webhook_process

    redis = get_redis()
    logger.info(f"[MQ] Consumer 启动: consumer_id={consumer_id}, queue={_QUEUE}, group={_GROUP}")

    while not stop_event.is_set():
        try:
            # XREADGROUP: 阻塞拉取未消费消息
            messages = await redis.xreadgroup(
                groupname=_GROUP,
                consumername=consumer_id,
                streams={_QUEUE: ">"},
                count=batch_size,
                block=block_ms,
            )

            if not messages:
                continue

            # messages 格式: [[stream_name, [(msg_id, {field: value}), ...]]]
            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    event_id_str = fields.get("event_id", "")
                    client_ip = fields.get("client_ip", "")

                    if not event_id_str:
                        logger.warning(f"[MQ] 消息缺少 event_id，跳过: msg_id={msg_id}")
                        await redis.xack(_QUEUE, _GROUP, msg_id)
                        continue

                    try:
                        event_id = int(event_id_str)
                    except (ValueError, TypeError):
                        logger.warning(f"[MQ] event_id 格式无效: {event_id_str}, msg_id={msg_id}")
                        await redis.xack(_QUEUE, _GROUP, msg_id)
                        continue

                    try:
                        await handle_webhook_process(event_id=event_id, client_ip=client_ip)
                        # 处理成功，ACK 确认
                        await redis.xack(_QUEUE, _GROUP, msg_id)
                    except Exception:
                        # 处理失败，不 ACK — 留在 PEL 供 RecoveryPoller 补偿
                        logger.error(
                            f"[MQ] 消息处理失败，留在 PEL 待重试: event_id={event_id}, msg_id={msg_id}",
                            exc_info=True,
                        )

        except asyncio.CancelledError:
            logger.info("[MQ] Consumer 收到取消信号，退出")
            break
        except Exception:
            logger.exception("[MQ] Consumer 循环异常，1s 后重试")
            # 避免异常风暴
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                pass

    logger.info("[MQ] Consumer 已停止")
