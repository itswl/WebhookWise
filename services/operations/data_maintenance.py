import asyncio
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import and_, delete, not_, or_, select, true
from sqlalchemy.exc import SQLAlchemyError

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
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
        "last_notified_at": event.last_notified_at,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "archived_at": archived_at,
    }


def _days_threshold(now: datetime, days: int) -> datetime:
    return now - timedelta(days=max(0, int(days)))


def _keyword_cleanup_filter(policy: DataMaintenancePolicy) -> sa.ColumnElement[bool] | None:
    conditions: list[sa.ColumnElement[bool]] = []
    for field, keywords in policy.cleanup_keywords.items():
        for keyword in keywords:
            if not keyword:
                continue
            if field == "summary":
                conditions.append(WebhookEvent.ai_analysis["summary"].astext.like(f"%{keyword}%"))
            elif field == "parsed_data":
                conditions.append(WebhookEvent.parsed_data.cast(sa.Text).like(f"%{keyword}%"))
            else:
                logger.warning("[Maintenance] Ignoring unknown cleanup keyword field: %s", field)
    if not conditions:
        return None
    return or_(*conditions)


def _does_not_match_policy(column: Any, policy_names: tuple[str, ...]) -> sa.ColumnElement[bool]:
    if not policy_names:
        return true()
    return or_(column.is_(None), not_(column.in_(policy_names)))


def _cleanup_filter(policy: DataMaintenancePolicy, now: datetime) -> sa.ColumnElement[bool]:
    """
    Build the retention predicate with explicit precedence:
    1. cleanup keywords shorten retention to the default retention window;
    2. configured importance retention owns known importance values;
    3. source retention applies only when no importance policy matches;
    4. default retention applies only when neither importance nor source policy matches.
    """
    conditions: list[sa.ColumnElement[bool]] = []

    importance_names = tuple(policy.retention_policies)
    source_names = tuple(policy.source_retention_policies)
    no_importance_policy = _does_not_match_policy(WebhookEvent.importance, importance_names)
    no_source_policy = _does_not_match_policy(WebhookEvent.source, source_names)

    keyword_filter = _keyword_cleanup_filter(policy)
    if keyword_filter is not None:
        conditions.append(
            and_(
                keyword_filter,
                WebhookEvent.timestamp < _days_threshold(now, policy.retention_days_default),
            )
        )

    for importance, days in policy.retention_policies.items():
        conditions.append(
            and_(
                WebhookEvent.importance == importance,
                WebhookEvent.timestamp < _days_threshold(now, days),
            )
        )

    for source, days in policy.source_retention_policies.items():
        conditions.append(
            and_(
                no_importance_policy,
                WebhookEvent.source == source,
                WebhookEvent.timestamp < _days_threshold(now, days),
            )
        )

    conditions.append(
        and_(
            no_importance_policy,
            no_source_policy,
            WebhookEvent.timestamp < _days_threshold(now, policy.retention_days_default),
        )
    )
    return or_(*conditions)


async def cleanup_old_data_by_policy(*, policy: DataMaintenancePolicy | None = None) -> int:
    """
    Archive and clean up expired webhook records according to the data retention policy.
    """
    policy = policy or DataMaintenancePolicy.from_config()
    if not policy.enabled:
        logger.info("[Maintenance] Data cleanup is disabled, skipping.")
        return 0

    total_archived = 0
    try:
        now = utcnow()

        combined_filter = _cleanup_filter(policy, now)

        batch_limit = 5000
        while True:
            deleted_this_round = 0
            async with session_scope() as session:
                # Find the IDs to process
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

                # Process in chunks (to avoid an overly large IN query)
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

            logger.info("[Maintenance] Archived and cleaned up %d records...", total_archived)
            if deleted_this_round < batch_limit:
                break
            await asyncio.sleep(0.5)

        if total_archived:
            logger.info("[Maintenance] Archive cleanup task complete! Processed %d records in total.", total_archived)
        else:
            logger.info("[Maintenance] No data needs to be cleaned up.")
        return total_archived

    except (RuntimeError, SQLAlchemyError, ValueError, TypeError) as e:
        logger.error("[Maintenance] Cleanup task failed: %s", e, exc_info=True)
        return total_archived
