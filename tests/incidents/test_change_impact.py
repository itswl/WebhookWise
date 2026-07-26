from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import ChangeEvent, Incident, WebhookEvent
from services.incidents.change_impact import assess_change_impact, get_change_impact


def _event(
    *,
    timestamp,
    service: str,
    dedup_key: str,
    importance: str = "medium",
) -> WebhookEvent:
    return WebhookEvent(
        source="grafana",
        timestamp=timestamp,
        parsed_data={
            "service": service,
            "environment": "prod",
            "alert_name": dedup_key,
        },
        dedup_key=dedup_key,
        importance=importance,
        processing_status="completed",
    )


@pytest.mark.asyncio
async def test_change_impact_explains_matching_before_after_growth(
    db_session: AsyncSession,
) -> None:
    started_at = utcnow() - timedelta(hours=2)
    change = ChangeEvent(
        source="github",
        external_id="deploy-42",
        change_type="deployment",
        service="checkout",
        environment="prod",
        started_at=started_at,
        status="succeeded",
    )
    db_session.add(change)
    db_session.add_all(
        [
            _event(
                timestamp=started_at - timedelta(minutes=10),
                service="checkout",
                dedup_key="latency",
            ),
            _event(
                timestamp=started_at + timedelta(minutes=2),
                service="checkout",
                dedup_key="latency",
            ),
            _event(
                timestamp=started_at + timedelta(minutes=4),
                service="checkout",
                dedup_key="errors",
                importance="high",
            ),
            _event(
                timestamp=started_at + timedelta(minutes=5),
                service="unrelated",
                dedup_key="ignored",
                importance="critical",
            ),
        ]
    )
    db_session.add(
        Incident(
            title="Checkout errors",
            source="grafana",
            status="active",
            workflow_status="open",
            started_at=started_at + timedelta(minutes=3),
            alert_count=2,
            top_importance="high",
            correlation_dimensions={"service": "checkout", "environment": "prod"},
        )
    )
    await db_session.commit()

    result = await assess_change_impact(db_session, change)

    assert result["status"] == "complete"
    assert result["level"] in {"medium", "high"}
    assert result["before_alert_count"] == 1
    assert result["after_alert_count"] == 2
    assert result["new_identity_count"] == 1
    assert result["linked_incident_count"] == 1
    assert "not proof of causation" in str(result["summary"])


@pytest.mark.asyncio
async def test_change_impact_reports_unknown_when_samples_are_insufficient(
    db_session: AsyncSession,
) -> None:
    change = ChangeEvent(
        source="gitlab",
        external_id="deploy-empty",
        change_type="deployment",
        service="catalog",
        environment="prod",
        started_at=utcnow() - timedelta(hours=2),
    )
    db_session.add(change)
    await db_session.commit()

    result = await assess_change_impact(db_session, change)

    assert result["status"] == "insufficient_data"
    assert result["level"] == "unknown"
    assert result["confidence"] < 1
    assert {"code": "insufficient_matching_samples", "value": 0} in result["evidence"]


@pytest.mark.asyncio
async def test_get_change_impact_returns_none_for_unknown_change(
    db_session: AsyncSession,
) -> None:
    assert await get_change_impact(db_session, 999_999) is None
