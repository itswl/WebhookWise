from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


@pytest.mark.asyncio
async def test_action_center_surfaces_current_operator_work_without_leaking_targets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import AuditLog, ForwardOutbox, ForwardRule, Incident, WebhookEvent
    from services.operations.action_center import get_action_center

    now = utcnow()
    async with session_factory.begin() as session:
        disabled_rule = ForwardRule(
            name="Feishu primary",
            enabled=False,
            target_type="feishu",
            target_url="https://open.feishu.cn/open-apis/bot/v2/hook/disabled-secret",
        )
        session.add(disabled_rule)
        await session.flush()
        session.add(
            AuditLog(
                resource_type="forward_rule",
                resource_id=disabled_rule.id,
                resource_name=disabled_rule.name,
                action="auto_disabled",
                summary="Forward rule auto-disabled after permanent delivery failure",
                actor="system",
                created_at=now,
            )
        )
        session.add_all(
            [
                ForwardOutbox(
                    idempotency_key="action-center-exhausted",
                    target_type="feishu",
                    target_url="https://open.feishu.cn/open-apis/bot/v2/hook/very-secret-token",
                    rule_name="secondary",
                    status="exhausted",
                    attempts=1,
                    max_attempts=3,
                    last_error=("request failed at https://open.feishu.cn/open-apis/bot/v2/hook/very-secret-token"),
                    response_data={"error_code": "19001", "retryable": False},
                    created_at=now,
                    updated_at=now,
                ),
                ForwardOutbox(
                    idempotency_key="action-center-exhausted-duplicate",
                    target_type="feishu",
                    target_url="https://open.feishu.cn/open-apis/bot/v2/hook/very-secret-token",
                    rule_name="secondary",
                    status="exhausted",
                    attempts=1,
                    max_attempts=3,
                    last_error="feishu business error code=19001: invalid token",
                    response_data={"error_code": "19001", "retryable": False},
                    created_at=now,
                    updated_at=now,
                ),
                ForwardOutbox(
                    idempotency_key="action-center-stale",
                    target_type="webhook",
                    target_url="https://example.test/hook",
                    status="pending",
                    attempts=0,
                    max_attempts=3,
                    # Due ten minutes ago and still not sent: genuinely late.
                    # Explicit, because relying on the column default made this
                    # row's dueness depend on insert ordering.
                    next_attempt_at=now - timedelta(minutes=10),
                    created_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=10),
                ),
                ForwardOutbox(
                    # A digest row HELD until its window closes is not late, and
                    # must not inflate the backlog count. See the digest test
                    # below for why this row is in the shared fixture too: the
                    # count assertion is what would have caught the regression.
                    idempotency_key="action-center-digest-held",
                    target_type="feishu",
                    target_url="https://example.test/digest",
                    rule_name="所有告警通知",
                    status="pending",
                    attempts=0,
                    max_attempts=3,
                    digest_key="rule:digest",
                    digest_window_end=now + timedelta(minutes=40),
                    next_attempt_at=now + timedelta(minutes=40),
                    created_at=now - timedelta(minutes=15),
                    updated_at=now - timedelta(minutes=15),
                ),
                WebhookEvent(
                    source="prometheus",
                    timestamp=now,
                    processing_status="dead_letter",
                    error_message="payload could not be parsed",
                ),
                WebhookEvent(
                    source="grafana",
                    timestamp=now - timedelta(minutes=30),
                    processing_status="analyzing",
                    updated_at=now - timedelta(minutes=30),
                ),
                Incident(
                    title="multi-alert incident",
                    status="quiet",
                    source="prometheus",
                    started_at=now,
                    alert_count=2,
                    summary_status="failed",
                    summary_last_error="provider authentication failed",
                    updated_at=now,
                ),
            ]
        )

    async with session_factory() as session:
        result = await get_action_center(session)

    kinds = {item["kind"] for item in result["items"]}
    assert {
        "integration_disabled",
        "delivery_exhausted",
        "dead_letter",
        "stuck_processing",
        "delivery_backlog",
        "ai_provider",
    } <= kinds
    assert result["summary"]["critical"] >= 3
    assert result["summary"]["dead_letters"] == 1
    # Only the genuinely-late row counts; the digest row held for its window does not.
    backlog_items = [item for item in result["items"] if item["kind"] == "delivery_backlog"]
    assert len(backlog_items) == 1
    assert backlog_items[0]["count"] == 1

    delivery_items = [item for item in result["items"] if item["kind"] == "delivery_exhausted"]
    assert len(delivery_items) == 1
    assert delivery_items[0]["count"] == 2
    assert delivery_items[0]["actions"] == []
    assert delivery_items[0]["title"] == "Permanent delivery fault: secondary"
    serialized = str(result)
    assert "very-secret-token" not in serialized
    assert "disabled-secret" not in serialized


@pytest.mark.asyncio
async def test_a_digest_row_held_for_its_window_is_not_a_delivery_backlog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A batched delivery is not a late one.

    The backlog item read "pending or retrying deliveries older than five
    minutes", which a digest row satisfies by design: it is held until its
    window closes. Measured on production 2026-09-09, all three pending rows
    were digest rows for rule "所有告警通知" with next_attempt_at =
    digest_window_end = 03:00, attempts 0 and no error. The window is hourly, so
    rows accumulated inside it all hour and the warning was effectively always
    on — which is how a real backlog would have gone unnoticed underneath it.
    """
    from models import ForwardOutbox
    from services.operations.action_center import get_action_center

    now = utcnow()
    async with session_factory.begin() as session:
        session.add_all(
            [
                ForwardOutbox(
                    idempotency_key="digest-window-open",
                    target_type="feishu",
                    rule_name="所有告警通知",
                    status="pending",
                    attempts=0,
                    digest_key="rule:1:window",
                    digest_window_end=now + timedelta(minutes=40),
                    next_attempt_at=now + timedelta(minutes=40),
                    created_at=now - timedelta(minutes=15),
                ),
                ForwardOutbox(
                    idempotency_key="retry-backoff-pending",
                    target_type="feishu",
                    rule_name="retrying",
                    status="retrying",
                    attempts=1,
                    # Waiting out its backoff: scheduled, not stuck.
                    next_attempt_at=now + timedelta(minutes=2),
                    created_at=now - timedelta(minutes=20),
                ),
            ]
        )

    async with session_factory() as session:
        result = await get_action_center(session)

    assert [item for item in result["items"] if item["kind"] == "delivery_backlog"] == []

    # The same row once its window has closed and it still has not gone out.
    async with session_factory.begin() as session:
        row = (
            await session.execute(select(ForwardOutbox).where(ForwardOutbox.idempotency_key == "digest-window-open"))
        ).scalar_one()
        row.digest_window_end = now - timedelta(minutes=6)
        row.next_attempt_at = now - timedelta(minutes=6)

    async with session_factory() as session:
        result = await get_action_center(session)

    backlog = [item for item in result["items"] if item["kind"] == "delivery_backlog"]
    assert len(backlog) == 1
    assert backlog[0]["count"] == 1
