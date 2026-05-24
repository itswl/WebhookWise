import asyncio
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import delete, or_, select

from core.logger import get_logger
from db.session import session_scope
from core.datetime_utils import utcnow
from models import ArchivedWebhookEvent, WebhookEvent
from services.operations.policies import DataMaintenancePolicy

logger = get_logger("maintenance")


def _archive_row(event: WebhookEvent, archived_at: datetime) -> dict[str, object]:
    return {
        "id": event.id,
        "request_id": event.request_id,
        "source": event.source,
        "client_ip": event.client_ip,
        "timestamp": event.timestamp,
        "raw_payload": event.raw_payload,
        "headers": event.headers,
        "parsed_data": event.parsed_data,
        "alert_hash": event.alert_hash,
        "ai_analysis": event.ai_analysis,
        "importance": event.importance,
        "processing_status": event.processing_status,
        "retry_count": event.retry_count,
        "next_retry_at": event.next_retry_at,
        "failure_reason": event.failure_reason,
        "error_message": event.error_message,
        "forward_status": event.forward_status,
        "prev_alert_id": event.prev_alert_id,
        "is_duplicate": event.is_duplicate,
        "duplicate_of": event.duplicate_of,
        "duplicate_count": event.duplicate_count,
        "beyond_window": False,
        "last_notified_at": event.last_notified_at,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "archived_at": archived_at,
    }


async def cleanup_old_data_by_policy(*, policy: DataMaintenancePolicy | None = None) -> int:
    """
    根据数据保留策略归档并清理过期 webhook 记录。
    """
    policy = policy or DataMaintenancePolicy.from_config()
    if not policy.enabled:
        logger.info("[Maintenance] 数据清理已禁用，跳过。")
        return 0

    total_archived = 0
    try:
        now = utcnow()

        # 1. 构建复合查询条件
        # 我们寻找符合以下任一条件的记录：
        # - 重要性匹配且超过保留天数
        # - 来源匹配且超过保留天数
        # - 超过默认全局保留天数

        conditions: list[sa.ColumnElement[bool]] = []

        # 按重要性策略
        for importance, days in policy.retention_policies.items():
            threshold = now - timedelta(days=days)
            conditions.append((WebhookEvent.importance == importance) & (WebhookEvent.timestamp < threshold))

        # 按来源策略
        for source, days in policy.source_retention_policies.items():
            threshold = now - timedelta(days=days)
            conditions.append((WebhookEvent.source == source) & (WebhookEvent.timestamp < threshold))

        # 按关键字匹配策略
        for field, keywords in policy.cleanup_keywords.items():
            for kw in keywords:
                if field == "summary":
                    conditions.append(WebhookEvent.ai_analysis["summary"].astext.like(f"%{kw}%"))
                elif field == "parsed_data":
                    conditions.append(WebhookEvent.parsed_data.cast(sa.Text).like(f"%{kw}%"))

        # 默认保留策略
        default_threshold = now - timedelta(days=policy.retention_days_default)
        # 如果既不在重要性策略里，也不在来源策略里，且超过默认天数，也清理
        # 但为了简单，我们直接加一个全局阈值作为主要判断逻辑之一
        conditions.append(WebhookEvent.timestamp < default_threshold)

        # 转换为 SQLAlchemy or_ 条件
        # 注意：这里可能产生重叠，但 or_ 会处理
        combined_filter = or_(*conditions)

        batch_limit = 5000
        while True:
            deleted_this_round = 0
            async with session_scope() as session:
                # 找出待处理的 ID
                target_ids = list(
                    (
                        await session.scalars(
                            select(WebhookEvent.id)
                            .filter(combined_filter)
                            .order_by(WebhookEvent.id.asc())
                            .limit(batch_limit)
                        )
                    ).all()
                )
                if not target_ids:
                    break

                # 分块处理 (避免过大的 IN 查询)
                for chunk_start in range(0, len(target_ids), 1000):
                    chunk_ids = target_ids[chunk_start : chunk_start + 1000]
                    events = list(
                        (
                            await session.scalars(
                                select(WebhookEvent)
                                .filter(WebhookEvent.id.in_(chunk_ids))
                                .order_by(WebhookEvent.id.asc())
                            )
                        ).all()
                    )
                    if not events:
                        continue

                    archived_at = utcnow()
                    archive_rows = [_archive_row(event, archived_at) for event in events]
                    await session.execute(sa.insert(ArchivedWebhookEvent), archive_rows)
                    await session.execute(delete(WebhookEvent).filter(WebhookEvent.id.in_(chunk_ids)))

                    deleted_this_round += len(events)
                    total_archived += len(events)

            logger.info("[Maintenance] 已转存并清理 %d 条记录...", total_archived)
            if deleted_this_round < batch_limit:
                break
            await asyncio.sleep(0.5)

        if total_archived:
            logger.info("[Maintenance] 转存清理任务完成！共处理 %d 条记录。", total_archived)
        else:
            logger.info("[Maintenance] 没有需要清理的数据。")
        return total_archived

    except Exception as e:
        logger.error("[Maintenance] 清理任务失败: %s", e, exc_info=True)
        return total_archived
