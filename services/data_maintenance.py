import asyncio
import logging
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import delete, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.config import Config
from db.session import session_scope
from models import ArchivedWebhookEvent, WebhookEvent

logger = logging.getLogger("webhook_service.maintenance")


async def archive_old_data_by_policy() -> int:
    """
    根据 Config.maintenance 中的保留策略，归档清理过期 webhook 记录。
    整合了之前 cleanup_alerts.py 的细粒度过滤逻辑。
    """
    if not Config.maintenance.ENABLE_ARCHIVE_CLEANUP:
        logger.info("[Maintenance] 数据归档已禁用，跳过。")
        return 0

    total_moved = 0
    try:
        now = datetime.now()

        # 1. 构建复合查询条件
        # 我们寻找符合以下任一条件的记录：
        # - 重要性匹配且超过保留天数
        # - 来源匹配且超过保留天数
        # - 超过默认全局保留天数

        conditions = []

        # 按重要性策略
        for importance, days in Config.maintenance.RETENTION_POLICIES.items():
            threshold = now - timedelta(days=days)
            conditions.append((WebhookEvent.importance == importance) & (WebhookEvent.timestamp < threshold))

        # 按来源策略
        for source, days in Config.maintenance.SOURCE_RETENTION_POLICIES.items():
            threshold = now - timedelta(days=days)
            conditions.append((WebhookEvent.source == source) & (WebhookEvent.timestamp < threshold))

        # 按关键字匹配策略 (来自 cleanup_general_events.py)
        for field, keywords in Config.maintenance.CLEANUP_KEYWORDS.items():
            for kw in keywords:
                if field == "summary":
                    conditions.append(WebhookEvent.ai_analysis["summary"].astext.like(f"%{kw}%"))
                elif field == "parsed_data":
                    conditions.append(WebhookEvent.parsed_data.cast(sa.Text).like(f"%{kw}%"))

        # 默认兜底策略
        default_threshold = now - timedelta(days=Config.maintenance.ARCHIVE_DAYS_DEFAULT)
        # 如果既不在重要性策略里，也不在来源策略里，且超过默认天数，也清理
        # 但为了简单，我们直接加一个全局阈值作为主要判断逻辑之一
        conditions.append(WebhookEvent.timestamp < default_threshold)

        # 转换为 SQLAlchemy or_ 条件
        # 注意：这里可能产生重叠，但 or_ 会处理
        combined_filter = or_(*list(conditions))

        batch_limit = 5000
        while True:
            moved_this_round = 0
            async with session_scope() as session:
                # 找出待处理的 ID
                result = await session.execute(
                    select(WebhookEvent.id).filter(combined_filter).order_by(WebhookEvent.id.asc()).limit(batch_limit)
                )
                target_ids = result.scalars().all()
                if not target_ids:
                    break

                # 分块处理 (避免过大的 IN 查询)
                for chunk_start in range(0, len(target_ids), 1000):
                    chunk_ids = target_ids[chunk_start : chunk_start + 1000]

                    # 获取完整对象
                    result = await session.execute(select(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)))
                    events = result.scalars().all()

                    archived_records = []
                    for e in events:
                        raw = e.raw_payload
                        if isinstance(raw, str):
                            raw = raw.encode("utf-8")

                        archived_records.append(
                            {
                                "id": e.id,
                                "source": e.source,
                                "client_ip": e.client_ip,
                                "timestamp": e.timestamp,
                                "raw_payload": raw,
                                "headers": e.headers,
                                "parsed_data": e.parsed_data,
                                "alert_hash": e.alert_hash,
                                "ai_analysis": e.ai_analysis,
                                "importance": e.importance,
                                "forward_status": e.forward_status,
                                "is_duplicate": e.is_duplicate,
                                "duplicate_of": e.duplicate_of,
                                "duplicate_count": e.duplicate_count,
                                "beyond_window": e.beyond_window,
                                "last_notified_at": e.last_notified_at,
                                "created_at": e.created_at,
                                "updated_at": e.updated_at,
                                "archived_at": datetime.now(),
                            }
                        )

                    if archived_records:
                        dialect_name = session.get_bind().dialect.name
                        if dialect_name == "postgresql":
                            stmt = (
                                pg_insert(ArchivedWebhookEvent)
                                .values(archived_records)
                                .on_conflict_do_nothing(index_elements=["id"])
                            )
                        elif dialect_name == "sqlite":
                            stmt = insert(ArchivedWebhookEvent).values(archived_records).prefix_with("OR IGNORE")
                        else:
                            stmt = insert(ArchivedWebhookEvent).values(archived_records)
                        await session.execute(stmt)

                    await session.execute(delete(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)))

                    moved_this_round += len(chunk_ids)
                    total_moved += len(chunk_ids)

            logger.info(f"[Maintenance] 已搬迁 {total_moved} 条记录...")
            if moved_this_round < batch_limit:
                break
            await asyncio.sleep(0.5)

        if total_moved:
            logger.info(f"[Maintenance] 归档任务完成！共处理 {total_moved} 条记录。")
        else:
            logger.info("[Maintenance] 没有需要归档的数据。")
        return total_moved

    except Exception as e:
        logger.error(f"[Maintenance] 归档任务失败: {e}", exc_info=True)
        return total_moved


# 保留原函数名以便向后兼容，但内部调用新逻辑
async def archive_old_data(archive_days: int = 30) -> int:
    """[已废弃] 请使用 archive_old_data_by_policy()"""
    return await archive_old_data_by_policy()
