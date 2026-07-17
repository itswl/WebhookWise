import asyncio
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import and_, delete, not_, or_, select, true, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import dml_rowcount, session_scope
from models import (
    AIUsageLog,
    ArchivedWebhookEvent,
    DeepAnalysis,
    ForwardOutbox,
    Incident,
    IncidentMember,
    WebhookEvent,
)
from services.operations.policies import DataMaintenancePolicy
from services.webhooks.types import ForwardOutboxStatus

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
        "dedup_key": event.dedup_key,
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
        "workflow_status": event.workflow_status,
        "assignee": event.assignee,
        "team": event.team,
        "acknowledged_at": event.acknowledged_at,
        "resolved_at": event.resolved_at,
        "sla_due_at": event.sla_due_at,
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


def _has_no_live_dependencies() -> sa.ColumnElement[bool]:
    """Protect records whose child data is still part of the operational history.

    Several child tables intentionally cascade when an event is deleted. Archival
    must not invoke those cascades before each child's own retention policy exists.
    Terminal outboxes become eligible after secondary retention removes them;
    incident members and deep analyses remain conservatively retained.
    """
    related_event = aliased(WebhookEvent)
    return and_(
        ~sa.exists(
            select(ForwardOutbox.id).where(
                or_(
                    ForwardOutbox.webhook_event_id == WebhookEvent.id,
                    ForwardOutbox.original_event_id == WebhookEvent.id,
                )
            )
        ),
        ~sa.exists(select(DeepAnalysis.id).where(DeepAnalysis.webhook_event_id == WebhookEvent.id)),
        ~sa.exists(select(IncidentMember.id).where(IncidentMember.event_id == WebhookEvent.id)),
        ~sa.exists(
            select(related_event.id).where(
                or_(
                    related_event.prev_alert_id == WebhookEvent.id,
                    related_event.duplicate_of == WebhookEvent.id,
                )
            )
        ),
    )


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

        combined_filter = and_(_cleanup_filter(policy, now), _has_no_live_dependencies())

        batch_limit = 5000
        while True:
            archived_this_round = 0
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

                    archived_this_round += len(events)

            # Count only after session_scope has committed the archive + delete.
            total_archived += archived_this_round
            logger.info("[Maintenance] Archived and cleaned up %d records...", total_archived)
            if archived_this_round < batch_limit:
                break
            await asyncio.sleep(0.5)

        if total_archived:
            logger.info("[Maintenance] Archive cleanup task complete! Processed %d records in total.", total_archived)
        else:
            logger.info("[Maintenance] No data needs to be cleaned up.")
        return total_archived

    except (RuntimeError, SQLAlchemyError, ValueError, TypeError) as e:
        logger.error("[Maintenance] Cleanup task failed: %s", e, exc_info=True)
        raise


async def cleanup_expired_operational_data(*, policy: DataMaintenancePolicy | None = None) -> dict[str, int]:
    """Bound secondary tables and close quiet incidents that no longer need attention."""
    policy = policy or DataMaintenancePolicy.from_config()
    if not policy.enabled:
        return {"archives": 0, "outboxes": 0, "ai_usage": 0, "incidents_closed": 0}

    now = utcnow()
    async with session_scope() as session:
        archives = await session.execute(
            delete(ArchivedWebhookEvent).where(
                ArchivedWebhookEvent.archived_at < _days_threshold(now, policy.archive_retention_days)
            )
        )
        outboxes = await session.execute(
            delete(ForwardOutbox).where(
                ForwardOutbox.status.in_(
                    [ForwardOutboxStatus.SENT, ForwardOutboxStatus.EXHAUSTED, ForwardOutboxStatus.EXPIRED]
                ),
                ForwardOutbox.updated_at < _days_threshold(now, policy.terminal_outbox_retention_days),
            )
        )
        ai_usage = await session.execute(
            delete(AIUsageLog).where(AIUsageLog.timestamp < _days_threshold(now, policy.ai_usage_retention_days))
        )
        incidents = await session.execute(
            update(Incident)
            .where(
                Incident.status == "quiet",
                Incident.updated_at < _days_threshold(now, policy.incident_auto_close_days),
            )
            .values(
                status="closed",
                workflow_status="resolved",
                resolved_at=now,
                ended_at=now,
                updated_at=now,
            )
        )

    result = {
        "archives": max(0, dml_rowcount(archives)),
        "outboxes": max(0, dml_rowcount(outboxes)),
        "ai_usage": max(0, dml_rowcount(ai_usage)),
        "incidents_closed": max(0, dml_rowcount(incidents)),
    }
    if any(result.values()):
        logger.info("[Maintenance] Secondary retention completed counts=%s", result)
    return result


async def run_data_maintenance(*, policy: DataMaintenancePolicy | None = None) -> dict[str, object]:
    """Run live-event archival and secondary retention as one scheduled job."""
    policy = policy or DataMaintenancePolicy.from_config()
    archived = await cleanup_old_data_by_policy(policy=policy)
    secondary = await cleanup_expired_operational_data(policy=policy)
    return {"events_archived": archived, **secondary}
