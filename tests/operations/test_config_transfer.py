"""Config bundle export/import: round-trip, upsert semantics, and exclusions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import ForwardRule, MaintenanceWindow, Silence
from services.operations.config_transfer import export_config, import_config


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_session_factory.begin() as sess:
        yield sess


async def _seed(session: AsyncSession) -> None:
    session.add(
        ForwardRule(
            name="feishu-primary",
            enabled=True,
            priority=5,
            match_importance="high",
            target_type="feishu",
            target_url="https://open.feishu.cn/open-apis/bot/v2/hook/x",
        )
    )
    session.add(Silence(match_source="zabbix", comment="planned migration", expires_at=utcnow() + timedelta(days=1)))
    # Expired / lifted / maintenance-materialized silences must NOT export.
    session.add(Silence(match_source="old", comment="expired", expires_at=utcnow() - timedelta(days=1)))
    session.add(Silence(match_source="mw", comment="[mw:1:2026-07-19] w", created_by="maintenance-window"))
    session.add(
        MaintenanceWindow(
            name="sunday-patch",
            enabled=True,
            match_source="zabbix",
            days_of_week="7",
            start_minute=120,
            duration_minutes=120,
            timezone="Asia/Shanghai",
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_export_shape_and_exclusions(session: AsyncSession) -> None:
    await _seed(session)
    bundle = await export_config(session)
    assert bundle["version"] == 1
    assert [r["name"] for r in bundle["forward_rules"]] == ["feishu-primary"]
    assert [s["comment"] for s in bundle["silences"]] == ["planned migration"]
    assert bundle["silences"][0]["expires_at"].endswith("Z") or "+" in bundle["silences"][0]["expires_at"]
    assert [w["name"] for w in bundle["maintenance_windows"]] == ["sunday-patch"]


@pytest.mark.asyncio
async def test_import_round_trip_is_idempotent(session: AsyncSession) -> None:
    await _seed(session)
    bundle = await export_config(session)

    report = await import_config(session, bundle)
    for collection in ("forward_rules", "silences", "maintenance_windows"):
        assert report[collection]["created"] == 0, collection
        assert report[collection]["updated"] == 0, collection
        assert report[collection]["errors"] == [], collection
        assert report[collection]["unchanged"] == 1, collection


@pytest.mark.asyncio
async def test_import_creates_and_updates_by_natural_key(session: AsyncSession) -> None:
    await _seed(session)
    bundle = {
        "version": 1,
        "forward_rules": [
            # Existing rule with a changed priority → update.
            {
                "name": "feishu-primary",
                "enabled": True,
                "priority": 9,
                "match_importance": "high",
                "target_type": "feishu",
                "target_url": "https://open.feishu.cn/open-apis/bot/v2/hook/x",
            },
            # New rule → create.
            {
                "name": "dingtalk-backup",
                "target_type": "webhook",
                "target_url": "https://oapi.dingtalk.com/robot/send?access_token=t",
            },
        ],
        "maintenance_windows": [
            {
                "name": "sunday-patch",
                "enabled": False,  # flipped → update
                "match_source": "zabbix",
                "days_of_week": "7",
                "start_minute": 120,
                "duration_minutes": 120,
                "timezone": "Asia/Shanghai",
            }
        ],
        "silences": [
            {"match_source": "grafana", "comment": "new import", "expires_at": "2027-01-01T00:00:00Z"},
        ],
    }
    report = await import_config(session, bundle)
    assert report["forward_rules"] == {"created": 1, "updated": 1, "unchanged": 0, "errors": []}
    assert report["maintenance_windows"]["updated"] == 1
    assert report["silences"]["created"] == 1

    rule_priority = (
        await session.execute(select(ForwardRule.priority).where(ForwardRule.name == "feishu-primary"))
    ).scalar_one()
    assert rule_priority == 9
    window_enabled = (
        await session.execute(select(MaintenanceWindow.enabled).where(MaintenanceWindow.name == "sunday-patch"))
    ).scalar_one()
    assert window_enabled is False
    imported = (await session.execute(select(Silence).where(Silence.comment == "new import"))).scalars().one()
    assert imported.expires_at is not None and imported.expires_at.year == 2027
    assert imported.expires_at.tzinfo is None  # stored naive-UTC like the rest of the app


@pytest.mark.asyncio
async def test_import_reports_errors_without_aborting(session: AsyncSession) -> None:
    await _seed(session)
    # Ambiguous rule name in target.
    session.add(ForwardRule(name="feishu-primary", target_type="webhook", target_url="https://example.com/dup"))
    await session.flush()
    bundle = {
        "version": 1,
        "forward_rules": [
            {"name": "feishu-primary", "target_type": "feishu"},
            {"name": "ok-rule", "target_type": "webhook", "target_url": "https://example.com/ok"},
        ],
        "silences": [{"comment": "no criteria at all"}],
    }
    report = await import_config(session, bundle)
    assert len(report["forward_rules"]["errors"]) == 1
    assert report["forward_rules"]["created"] == 1  # the good entry still lands
    assert len(report["silences"]["errors"]) == 1


@pytest.mark.asyncio
async def test_import_rejects_wrong_version(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="unsupported bundle version"):
        await import_config(session, {"version": 99})
    with pytest.raises(ValueError, match="mapping"):
        await import_config(session, ["not-a-dict"])
