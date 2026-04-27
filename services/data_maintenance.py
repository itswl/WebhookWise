import logging
from datetime import datetime, timedelta

from core.config import Config
from db.session import session_scope

logger = logging.getLogger('webhook_service.maintenance')


async def archive_old_data(archive_days: int = 30) -> int:
    """
    归档清理过期 webhook 记录到归档表，保持主表轻量
    """
    if not getattr(Config, "ENABLE_ARCHIVE_CLEANUP", True):
        logger.info("[Maintenance] 数据归档已禁用，跳过。")
        return 0

    try:
        from sqlalchemy import delete, insert, select

        from models import ArchivedWebhookEvent, WebhookEvent

        threshold_date = datetime.now() - timedelta(days=archive_days)
        logger.info(f"[Maintenance] 准备归档 {threshold_date.date()} 之前的数据...")

        total_moved = 0
        async with session_scope() as session:
            # 1. 查找需要归档的 ID (每次最多 5000 条，避免内存过大)
            result = await session.execute(select(WebhookEvent.id).filter(WebhookEvent.timestamp < threshold_date).limit(5000))
            target_ids = result.scalars().all()

            if not target_ids:
                logger.info("[Maintenance] 没有需要归档的数据。")
                return 0

            # 2. 分批处理
            for chunk_start in range(0, len(target_ids), 1000):
                chunk_ids = target_ids[chunk_start : chunk_start + 1000]

                result = await session.execute(select(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)))
                events = result.scalars().all()

                archived_records = [{
                    'id': e.id,
                    'source': e.source,
                    'client_ip': e.client_ip,
                    'timestamp': e.timestamp,
                    'raw_payload': e.raw_payload,
                    'headers': e.headers,
                    'parsed_data': e.parsed_data,
                    'alert_hash': e.alert_hash,
                    'ai_analysis': e.ai_analysis,
                    'importance': e.importance,
                    'forward_status': e.forward_status,
                    'is_duplicate': e.is_duplicate,
                    'duplicate_of': e.duplicate_of,
                    'duplicate_count': e.duplicate_count,
                    'beyond_window': e.beyond_window,
                    'last_notified_at': e.last_notified_at,
                    'created_at': e.created_at,
                    'updated_at': e.updated_at,
                    'archived_at': datetime.now()
                } for e in events]

                if archived_records:
                    await session.execute(insert(ArchivedWebhookEvent), archived_records)

                await session.execute(delete(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)))

                total_moved += len(chunk_ids)
                logger.info(f"[Maintenance] 已搬迁 {total_moved} 条记录...")

            logger.info(f"[Maintenance] 归档任务完成！共处理 {total_moved} 条记录。")
            return total_moved

    except Exception as e:
        logger.error(f"[Maintenance] 归档任务失败: {e}", exc_info=True)
        return total_moved
