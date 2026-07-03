"""Tests for the alert-health periodic reports (daily / weekly / monthly)."""

import contextlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.datetime_utils import utcnow
from db.session import Base
from models import AIUsageLog, WebhookEvent


@contextlib.asynccontextmanager
async def _noop_session_scope() -> AsyncIterator[None]:
    """Stand-in for db.session_scope so report tests don't open a real engine."""
    yield None


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_collect_report_stats_aggregates_noise_sources_and_cost(session: AsyncSession) -> None:
    from services.operations.periodic_report import collect_report_stats

    now = utcnow()
    # 10 events: 3 duplicates; sources prometheus x6, grafana x4; mixed importance.
    for i in range(10):
        session.add(
            WebhookEvent(
                source="prometheus" if i < 6 else "grafana",
                importance="high" if i < 2 else "low",
                is_duplicate=i < 3,
                timestamp=now,
                duplicate_count=1,
            )
        )
    # AI usage: 2 calls, one a cache hit, total cost 0.05.
    session.add(AIUsageLog(timestamp=now, model="m", cost_estimate=0.03, cache_hit=False))
    session.add(AIUsageLog(timestamp=now, model="m", cost_estimate=0.02, cache_hit=True))
    await session.commit()

    stats = await collect_report_stats(session, window_days=7)

    assert stats["total_events"] == 10
    assert stats["duplicate_events"] == 3
    assert stats["noise_pct"] == 30.0
    assert stats["top_sources"][0] == {"source": "prometheus", "count": 6}
    assert stats["importance_breakdown"] == {"high": 2, "low": 8}
    assert stats["ai_calls"] == 2
    assert stats["ai_cost_usd"] == 0.05
    assert stats["cache_hit_pct"] == 50.0


@pytest.mark.asyncio
async def test_top_rules_breaks_down_noisiest_source_by_rule(session: AsyncSession) -> None:
    """'source' (e.g. volcengine) is too coarse — the report must break the
    noisiest source down by its alert rule name."""
    from services.operations.periodic_report import collect_report_stats

    now = utcnow()
    # volcengine is noisiest (5), dominated by one rule (4x GPU vs 1x storage).
    for _ in range(4):
        session.add(WebhookEvent(source="volcengine", timestamp=now, duplicate_count=1,
                                 parsed_data={"RuleName": "GPU卡告警", "Type": "Metric"}))
    session.add(WebhookEvent(source="volcengine", timestamp=now, duplicate_count=1,
                             parsed_data={"RuleName": "对象存储告警", "Type": "Metric"}))
    session.add(WebhookEvent(source="grafana", timestamp=now, duplicate_count=1,
                             parsed_data={"RuleName": "x"}))
    await session.commit()

    stats = await collect_report_stats(session, window_days=7)
    assert stats["top_sources"][0]["source"] == "volcengine"
    rules = {r["rule"]: r["count"] for r in stats["top_rules"]}
    assert rules["GPU卡告警"] == 4
    assert rules["对象存储告警"] == 1
    assert all(r["source"] == "volcengine" for r in stats["top_rules"])


@pytest.mark.asyncio
async def test_collect_report_stats_excludes_events_outside_window(session: AsyncSession) -> None:
    from datetime import timedelta

    from services.operations.periodic_report import collect_report_stats

    now = utcnow()
    session.add(WebhookEvent(source="s", timestamp=now, duplicate_count=1))
    session.add(WebhookEvent(source="s", timestamp=now - timedelta(days=30), duplicate_count=1))
    await session.commit()

    stats = await collect_report_stats(session, window_days=7)
    assert stats["total_events"] == 1  # the 30-day-old one is excluded


@pytest.mark.asyncio
async def test_weekly_report_no_op_when_disabled(temp_config) -> None:
    from services.operations.periodic_report import generate_and_send_report

    temp_config.notifications.WEEKLY_REPORT_ENABLED = False
    result = await generate_and_send_report("weekly")
    assert result == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_weekly_report_skips_when_no_webhook(temp_config) -> None:
    from services.operations.periodic_report import generate_and_send_report

    temp_config.notifications.WEEKLY_REPORT_ENABLED = True
    temp_config.notifications.WEEKLY_REPORT_FEISHU_WEBHOOK = ""
    temp_config.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK = ""
    result = await generate_and_send_report("weekly")
    assert result == {"skipped": "no_webhook"}


def test_build_summary_is_deterministic_and_human_readable() -> None:
    from services.operations.periodic_report import _build_summary

    stats = {
        "window_days": 7,
        "total_events": 100,
        "duplicate_events": 40,
        "noise_pct": 40.0,
        "importance_breakdown": {"high": 10, "low": 90},
        "top_sources": [{"source": "prometheus", "count": 55}],
        "ai_cost_usd": 1.23,
        "ai_calls": 60,
        "cache_hit_pct": 25.0,
    }
    text = _build_summary(stats)
    assert "100" in text and "40.0%" in text and "prometheus" in text and "$1.23" in text


@pytest.mark.parametrize(
    ("period_key", "enabled_attr", "window_attr", "webhook_attr", "title_word"),
    [
        ("daily", "DAILY_REPORT_ENABLED", "DAILY_REPORT_WINDOW_DAYS", "DAILY_REPORT_FEISHU_WEBHOOK", "Daily"),
        ("weekly", "WEEKLY_REPORT_ENABLED", "WEEKLY_REPORT_WINDOW_DAYS", "WEEKLY_REPORT_FEISHU_WEBHOOK", "Weekly"),
        ("monthly", "MONTHLY_REPORT_ENABLED", "MONTHLY_REPORT_WINDOW_DAYS", "MONTHLY_REPORT_FEISHU_WEBHOOK", "Monthly"),
    ],
)
def test_report_periods_registry_matches_config(period_key, enabled_attr, window_attr, webhook_attr, title_word) -> None:
    from services.operations.periodic_report import REPORT_PERIODS

    period = REPORT_PERIODS[period_key]
    assert period.enabled_attr == enabled_attr
    assert period.window_attr == window_attr
    assert period.webhook_attr == webhook_attr
    assert title_word in period.title


@pytest.mark.asyncio
@pytest.mark.parametrize("period_key", ["daily", "weekly", "monthly"])
async def test_report_no_op_when_disabled(temp_config, period_key) -> None:
    from services.operations.periodic_report import REPORT_PERIODS, generate_and_send_report

    setattr(temp_config.notifications, REPORT_PERIODS[period_key].enabled_attr, False)
    result = await generate_and_send_report(period_key)
    assert result == {"skipped": "disabled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("period_key", ["daily", "weekly", "monthly"])
async def test_report_skips_when_no_webhook(temp_config, period_key) -> None:
    from services.operations.periodic_report import REPORT_PERIODS, generate_and_send_report

    notif = temp_config.notifications
    setattr(notif, REPORT_PERIODS[period_key].enabled_attr, True)
    setattr(notif, REPORT_PERIODS[period_key].webhook_attr, "")
    notif.WEEKLY_REPORT_FEISHU_WEBHOOK = ""
    notif.DEEP_ANALYSIS_FEISHU_WEBHOOK = ""
    result = await generate_and_send_report(period_key)
    assert result == {"skipped": "no_webhook"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period_key", "title_word"),
    [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")],
)
async def test_report_sends_card_with_period_title(temp_config, monkeypatch, period_key, title_word) -> None:
    """Each cadence sends a card titled for its period, using its window + webhook."""
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import REPORT_PERIODS, generate_and_send_report

    notif = temp_config.notifications
    setattr(notif, REPORT_PERIODS[period_key].enabled_attr, True)
    setattr(notif, REPORT_PERIODS[period_key].webhook_attr, "https://example.com/hook")

    sent: dict[str, object] = {}

    async def fake_collect(_session, window_days):
        sent["window_days"] = window_days
        return {
            "window_days": window_days,
            "total_events": 0,
            "duplicate_events": 0,
            "noise_pct": 0.0,
            "importance_breakdown": {},
            "top_sources": [],
            "top_rules": [],
            "ai_cost_usd": 0.0,
            "ai_calls": 0,
            "cache_hit_pct": 0.0,
        }

    async def fake_send(url, card):
        sent["url"] = url
        sent["title"] = card["card"]["header"]["title"]["content"]
        return {"status": "success"}

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "collect_report_stats", fake_collect)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", fake_send)

    await generate_and_send_report(period_key)

    assert sent["url"] == "https://example.com/hook"
    assert title_word in sent["title"]
    assert sent["window_days"] == getattr(notif, REPORT_PERIODS[period_key].window_attr)


@pytest.mark.asyncio
async def test_report_webhook_falls_back_to_weekly_then_deep_analysis(temp_config, monkeypatch) -> None:
    """Daily report with no dedicated webhook falls back to the weekly webhook."""
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import generate_and_send_report

    notif = temp_config.notifications
    notif.DAILY_REPORT_ENABLED = True
    notif.DAILY_REPORT_FEISHU_WEBHOOK = ""
    notif.WEEKLY_REPORT_FEISHU_WEBHOOK = "https://example.com/weekly-hook"
    notif.DEEP_ANALYSIS_FEISHU_WEBHOOK = "https://example.com/deep-hook"

    captured: dict[str, str] = {}

    async def fake_collect(_session, window_days):
        return {
            "window_days": window_days, "total_events": 0, "duplicate_events": 0,
            "noise_pct": 0.0, "importance_breakdown": {}, "top_sources": [],
            "top_rules": [], "ai_cost_usd": 0.0, "ai_calls": 0, "cache_hit_pct": 0.0,
        }

    async def fake_send(url, card):
        captured["url"] = url
        return {"status": "success"}

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "collect_report_stats", fake_collect)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", fake_send)

    await generate_and_send_report("daily")
    assert captured["url"] == "https://example.com/weekly-hook"


# ── Missed-fire catch-up ──────────────────────────────────────────────────────

from datetime import UTC, datetime  # noqa: E402


def test_most_recent_fire_finds_last_daily_match() -> None:
    from services.operations.periodic_report import _most_recent_fire

    # Crons are evaluated in Asia/Shanghai (UTC+8): "0 9" = 09:00 Beijing = 01:00 UTC.
    # now = 2026-06-16 14:30 UTC → most recent daily fire is 2026-06-16 01:00 UTC.
    now = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)
    fire = _most_recent_fire("0 9 * * *", now, 24 * 60 + 60)
    assert fire == datetime(2026, 6, 16, 1, 0, tzinfo=UTC)


def test_most_recent_fire_weekly_walks_back_to_monday() -> None:
    from services.operations.periodic_report import _most_recent_fire

    # Weekly Mon 09:00 Beijing = Mon 01:00 UTC. now = Wed 2026-06-17 10:00 UTC
    # → most recent is Monday 2026-06-15 01:00 UTC.
    now = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    fire = _most_recent_fire("0 9 * * 1", now, 7 * 24 * 60 + 60)
    assert fire == datetime(2026, 6, 15, 1, 0, tzinfo=UTC)


def test_most_recent_fire_none_when_outside_lookback() -> None:
    from services.operations.periodic_report import _most_recent_fire

    # Monthly 1st 09:00, now is the 16th, but a tiny 10-minute lookback can't reach it.
    now = datetime(2026, 6, 16, 14, 30, tzinfo=UTC)
    assert _most_recent_fire("0 9 1 * *", now, 10) is None


@pytest.mark.asyncio
async def test_catchup_sends_when_missed_and_skips_when_already_sent(temp_config, monkeypatch) -> None:
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import run_report_catchup

    notif = temp_config.notifications
    # Only daily enabled, with a webhook.
    notif.DAILY_REPORT_ENABLED = True
    notif.WEEKLY_REPORT_ENABLED = False
    notif.MONTHLY_REPORT_ENABLED = False
    notif.DAILY_REPORT_FEISHU_WEBHOOK = "https://example.com/hook"

    sends: list[str] = []
    marker: dict[str, datetime] = {}

    async def fake_collect(_session, window_days):
        return {
            "window_days": window_days, "total_events": 0, "duplicate_events": 0,
            "noise_pct": 0.0, "importance_breakdown": {}, "top_sources": [],
            "top_rules": [], "ai_cost_usd": 0.0, "ai_calls": 0, "cache_hit_pct": 0.0,
        }

    async def fake_send(url, card):
        sends.append(url)
        return {"status": "success"}

    async def fake_record(period_key, fire_ts):
        marker[period_key] = fire_ts

    async def fake_last_sent(period_key):
        return marker.get(period_key)

    async def fake_claim(period_key, fire):
        return True

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "collect_report_stats", fake_collect)
    monkeypatch.setattr(wr, "_record_report_sent", fake_record)
    monkeypatch.setattr(wr, "_last_sent_fire", fake_last_sent)
    monkeypatch.setattr(wr, "_claim_catchup", fake_claim)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", fake_send)

    # First run: nothing sent yet → catch-up fires once.
    out1 = await run_report_catchup()
    assert out1["daily"] == "sent"
    assert len(sends) == 1

    # Second run (e.g. another restart same day): already sent → no duplicate.
    out2 = await run_report_catchup()
    assert out2["daily"] == "already_sent"
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_catchup_single_flight_skips_when_claim_lost(temp_config, monkeypatch) -> None:
    """If another worker already claimed the occurrence, this one does not send."""
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import run_report_catchup

    notif = temp_config.notifications
    notif.DAILY_REPORT_ENABLED = True
    notif.WEEKLY_REPORT_ENABLED = False
    notif.MONTHLY_REPORT_ENABLED = False
    notif.DAILY_REPORT_FEISHU_WEBHOOK = "https://example.com/hook"

    sends: list[str] = []

    async def fake_send(url, card):
        sends.append(url)
        return {"status": "success"}

    async def no_marker(period_key):
        return None

    async def lost_claim(period_key, fire):
        return False

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "_last_sent_fire", no_marker)
    monkeypatch.setattr(wr, "_claim_catchup", lost_claim)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", fake_send)

    out = await run_report_catchup()
    assert out["daily"] == "claimed_elsewhere"
    assert sends == []


@pytest.mark.asyncio
async def test_report_send_retries_transient_then_succeeds(temp_config, monkeypatch) -> None:
    """A transient Feishu failure (e.g. 11232 frequency limited) is retried, not lost."""
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import generate_and_send_report

    notif = temp_config.notifications
    notif.DAILY_REPORT_ENABLED = True
    notif.DAILY_REPORT_FEISHU_WEBHOOK = "https://example.com/hook"

    attempts: list[int] = []

    async def flaky_send(url, card):
        attempts.append(1)
        if len(attempts) < 3:
            return {"status": "failed", "message": "feishu business error code=11232: frequency limited"}
        return {"status": "success"}

    async def fake_collect(_session, window_days):
        return {
            "window_days": window_days, "total_events": 0, "duplicate_events": 0, "noise_pct": 0.0,
            "importance_breakdown": {}, "top_sources": [], "top_rules": [], "ai_cost_usd": 0.0,
            "ai_calls": 0, "cache_hit_pct": 0.0,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "collect_report_stats", fake_collect)
    monkeypatch.setattr(wr.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", flaky_send)

    await generate_and_send_report("daily")

    # Two failures then success → exactly 3 attempts, report not dropped.
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_report_send_does_not_retry_invalid_target(temp_config, monkeypatch) -> None:
    """A misconfigured URL is not retried (retrying can't fix config)."""
    import services.operations.periodic_report as wr
    from services.operations.periodic_report import generate_and_send_report

    notif = temp_config.notifications
    notif.DAILY_REPORT_ENABLED = True
    notif.DAILY_REPORT_FEISHU_WEBHOOK = "https://example.com/hook"

    attempts: list[int] = []

    async def invalid_send(url, card):
        attempts.append(1)
        return {"status": "invalid_target", "message": "bad url"}

    async def fake_collect(_session, window_days):
        return {
            "window_days": window_days, "total_events": 0, "duplicate_events": 0, "noise_pct": 0.0,
            "importance_breakdown": {}, "top_sources": [], "top_rules": [], "ai_cost_usd": 0.0,
            "ai_calls": 0, "cache_hit_pct": 0.0,
        }

    monkeypatch.setattr(wr, "session_scope", _noop_session_scope)
    monkeypatch.setattr(wr, "collect_report_stats", fake_collect)
    monkeypatch.setattr("services.notifications.feishu.send_to_feishu", invalid_send)

    await generate_and_send_report("daily")
    assert len(attempts) == 1  # no retry on invalid_target


@pytest.mark.asyncio
async def test_last_sent_fire_coerces_naive_marker_to_utc(monkeypatch) -> None:
    """A stale naive last-sent marker is read back tz-aware (so catch-up can't crash)."""
    from datetime import UTC, datetime

    import services.operations.periodic_report as wr

    # A naive isoformat (written by older code) and an aware one must BOTH come
    # back tz-aware and compare against an aware datetime without raising.
    for raw in ("2026-06-18T01:00:00", "2026-06-18T01:00:00+00:00"):
        async def _get(_key, _raw=raw):
            return _raw

        monkeypatch.setattr("core.redis_client.redis_get_str", _get)
        result = await wr._last_sent_fire("daily")
        assert result is not None
        assert result.tzinfo is not None, f"marker {raw!r} should come back tz-aware"
        # The comparison that crashed in prod (`last_sent >= fire`) must not raise.
        assert isinstance(result >= datetime.now(tz=UTC), bool)
