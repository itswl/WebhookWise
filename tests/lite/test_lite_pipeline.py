"""WebhookWise Lite: the suppression chain and the trace it leaves behind.

These are the contract of the lite edition — each gate must stop the alert for
its OWN named reason, because the whole promise is that "why didn't I get
notified" has a precise answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lite import pipeline
from lite.normalize import normalize
from lite.store import Store


@dataclass(frozen=True)
class _Settings:
    dedup_window_seconds: int = 300
    cooldown_seconds: int = 1800
    openai_api_key: str = ""
    openai_api_url: str = ""
    openai_model: str = ""
    ai_timeout_seconds: int = 5


@pytest.fixture
async def store(tmp_path: Any):
    store = Store(str(tmp_path / "lite.db"))
    await store.open()
    yield store
    await store.close()


@pytest.fixture
async def routed_store(store: Store):
    await store.add_rule({"name": "catch-all", "target_kind": "generic", "target_url": "http://sink.invalid/hook"})
    return store


ALERT = {"title": "disk full on db-01", "body": "/ is at 95%"}


async def _run(store: Store, payload: dict[str, Any], source: str = "prod", **overrides: Any) -> dict[str, Any]:
    return await pipeline.process(store, None, _Settings(**overrides), source, payload)


@pytest.mark.asyncio
async def test_forwarded_alert_enqueues_delivery_and_records_why(routed_store: Store) -> None:
    result = await _run(routed_store, ALERT)

    assert result["outcome"] == "forwarded"
    assert result["rules"] == ["catch-all"]
    assert len(await routed_store.due_deliveries()) == 1

    trace = (await routed_store.list_decisions())[0]
    assert [step["step"] for step in trace["steps"]] == [
        "normalize",
        "dedup",
        "silence",
        "analysis",
        "rules",
        "cooldown",
        "forward",
    ]


@pytest.mark.asyncio
async def test_identical_alert_inside_the_window_is_a_duplicate(routed_store: Store) -> None:
    await _run(routed_store, ALERT)
    result = await _run(routed_store, ALERT)

    assert result["skip_code"] == "duplicate"
    # A suppressed alert must not produce a second delivery.
    assert len(await routed_store.due_deliveries()) == 1
    dedup_step = next(s for s in (await routed_store.list_decisions())[0]["steps"] if s["step"] == "dedup")
    assert dedup_step["result"] == "duplicate"


@pytest.mark.asyncio
async def test_cooldown_paces_renotification_once_dedup_has_expired(routed_store: Store) -> None:
    """The gate that only exists when the dedup window is the SHORTER one."""
    await _run(routed_store, ALERT, dedup_window_seconds=0)
    result = await _run(routed_store, ALERT, dedup_window_seconds=0, cooldown_seconds=3600)

    assert result["skip_code"] == "cooldown"


@pytest.mark.asyncio
async def test_cooldown_is_per_identity_not_global(routed_store: Store) -> None:
    """A different alert must not inherit another alert's cooldown."""
    await _run(routed_store, ALERT)
    result = await _run(routed_store, {"title": "cpu spike on web-02", "body": "99%"})

    assert result["outcome"] == "forwarded"


@pytest.mark.asyncio
async def test_silence_suppresses_before_any_ai_call(routed_store: Store) -> None:
    await routed_store.add_silence("noisy-job", minutes=30, reason="known flaky")
    result = await _run(routed_store, {"title": "noisy-job failed", "body": "retrying"})

    assert result["skip_code"] == "silenced"
    steps = {step["step"] for step in (await routed_store.list_decisions())[0]["steps"]}
    # Analysis must not have run: a silenced alert should cost nothing.
    assert "analysis" not in steps


@pytest.mark.asyncio
async def test_expired_silence_no_longer_suppresses(store: Store) -> None:
    await store.add_rule({"name": "catch-all", "target_kind": "generic", "target_url": "http://sink.invalid/hook"})
    await store.add_silence("noisy-job", minutes=-1)  # already expired
    result = await _run(store, {"title": "noisy-job failed", "body": "retrying"})

    assert result["outcome"] == "forwarded"


@pytest.mark.asyncio
async def test_unrouted_alert_is_skipped_as_no_match(store: Store) -> None:
    result = await _run(store, ALERT)
    assert result["skip_code"] == "no_match"


@pytest.mark.asyncio
async def test_rules_filter_on_source_and_importance(store: Store) -> None:
    await store.add_rule(
        {
            "name": "prod-high-only",
            "match_source": "prod",
            "match_importance": "high",
            "target_kind": "generic",
            "target_url": "http://sink.invalid/hook",
        }
    )
    # "critical" in the body drives rule triage to high.
    assert (await _run(store, {"title": "outage", "body": "critical"}, source="prod"))["outcome"] == "forwarded"
    assert (await _run(store, {"title": "outage", "body": "critical"}, source="staging"))["skip_code"] == "no_match"


@pytest.mark.asyncio
async def test_every_alert_produces_exactly_one_decision(routed_store: Store) -> None:
    await _run(routed_store, ALERT)
    await _run(routed_store, ALERT)
    await _run(routed_store, {"title": "another", "body": "x"})

    decisions = await routed_store.list_decisions()
    assert len(decisions) == 3
    assert sorted(d["skip_code"] for d in decisions) == ["duplicate", "none", "none"]


# ── normalization ─────────────────────────────────────────────────────────────


def test_alertmanager_identity_survives_description_edits() -> None:
    def payload(description: str) -> dict[str, Any]:
        return {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighLatency", "instance": "web-01"},
                    "annotations": {"summary": "latency high", "description": description},
                }
            ]
        }

    first = normalize("prom", payload("p99 at 1.2s"))
    second = normalize("prom", payload("p99 at 3.4s"))
    # Identity comes from the label set, so a changing description is still the
    # same alert and still dedups.
    assert first["alert_hash"] == second["alert_hash"]


def test_resolved_notice_is_a_distinct_identity_from_its_firing_alert() -> None:
    labels = {"alertname": "HighLatency", "instance": "web-01"}
    firing = normalize("prom", {"alerts": [{"status": "firing", "labels": labels, "annotations": {}}]})
    resolved = normalize("prom", {"alerts": [{"status": "resolved", "labels": labels, "annotations": {}}]})

    # Sharing the identity would make the recovery look like a duplicate of the
    # alert it closes, and the recovery would never be delivered.
    assert firing["alert_hash"] != resolved["alert_hash"]
    assert resolved["resolved"] is True


def test_unparseable_payload_still_becomes_an_alert() -> None:
    event = normalize("weird", {"nothing": "recognizable"})
    assert event["title"] and event["body"]


@pytest.mark.asyncio
async def test_delivery_retries_then_exhausts(store: Store) -> None:
    await store.enqueue_delivery(1, "r", "generic", "http://sink.invalid/hook", {"x": 1})
    outbox_id = (await store.due_deliveries())[0]["id"]

    statuses = [await store.mark_failed(int(outbox_id), "boom", 0) for _ in range(4)]
    assert statuses == ["pending", "pending", "pending", "exhausted"]
    assert (await store.outbox_summary())["exhausted"] == 1


@pytest.mark.asyncio
async def test_backoff_defers_the_next_attempt(store: Store) -> None:
    await store.enqueue_delivery(1, "r", "generic", "http://sink.invalid/hook", {"x": 1})
    outbox_id = (await store.due_deliveries())[0]["id"]

    await store.mark_failed(int(outbox_id), "boom", 60)
    assert await store.due_deliveries() == []  # not due again yet


def test_env_example_documents_every_setting() -> None:
    """The template is the only config reference lite has — it must not drift.

    Checked in both directions: an undocumented setting is invisible to
    operators, and a documented-but-nonexistent one sends them chasing a
    variable that does nothing.
    """
    import re
    from pathlib import Path

    from lite.settings import Settings

    root = Path(__file__).resolve().parents[2]
    source = (root / "lite/settings.py").read_text()
    used = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"|_int\("([A-Z_]+)"', source))
    in_code = {name for pair in used for name in pair if name}

    example = (root / "lite/.env.example").read_text()
    documented = set(re.findall(r"^([A-Z_]+)=", example, re.MULTILINE))

    assert sorted(in_code - documented) == [], "settings missing from .env.example"
    assert sorted(documented - in_code) == [], ".env.example documents unknown settings"
    # Every field on Settings must come from one of those variables.
    assert len(in_code) == len(Settings.__dataclass_fields__)


@pytest.mark.asyncio
async def test_priority_orders_rule_evaluation(store: Store) -> None:
    await store.add_rule({"name": "low-prio", "target_kind": "generic", "target_url": "http://a", "priority": 1})
    await store.add_rule({"name": "high-prio", "target_kind": "generic", "target_url": "http://b", "priority": 10})

    assert [r["name"] for r in await store.active_rules()] == ["high-prio", "low-prio"]


@pytest.mark.asyncio
async def test_stop_on_match_expresses_everything_else(store: Store) -> None:
    """The routing shape that disjoint match sets cannot express cleanly."""
    await store.add_rule(
        {
            "name": "oncall",
            "match_importance": "high",
            "target_kind": "generic",
            "target_url": "http://oncall",
            "priority": 10,
            "stop_on_match": True,
        }
    )
    await store.add_rule({"name": "general", "target_kind": "generic", "target_url": "http://general", "priority": 0})

    high = await _run(store, {"title": "outage", "body": "critical"})
    assert high["rules"] == ["oncall"]  # general was never reached

    low = await _run(store, {"title": "backup done", "body": "info notice"})
    assert low["rules"] == ["general"]


@pytest.mark.asyncio
async def test_early_stop_is_visible_in_the_trace(store: Store) -> None:
    """A rule that never got evaluated must still be explainable."""
    await store.add_rule(
        {
            "name": "oncall",
            "target_kind": "generic",
            "target_url": "http://oncall",
            "priority": 10,
            "stop_on_match": True,
        }
    )
    await store.add_rule({"name": "archive", "target_kind": "generic", "target_url": "http://archive"})

    await _run(store, ALERT)
    rules_step = next(s for s in (await store.list_decisions())[0]["steps"] if s["step"] == "rules")
    assert rules_step["stopped_by"] == "oncall"


@pytest.mark.asyncio
async def test_fan_out_without_stop_reaches_every_matching_rule(store: Store) -> None:
    await store.add_rule({"name": "oncall", "target_kind": "generic", "target_url": "http://oncall", "priority": 10})
    await store.add_rule({"name": "archive", "target_kind": "generic", "target_url": "http://archive"})

    result = await _run(store, ALERT)
    assert result["rules"] == ["oncall", "archive"]
    assert len(await store.due_deliveries()) == 2
    rules_step = next(s for s in (await store.list_decisions())[0]["steps"] if s["step"] == "rules")
    assert rules_step["stopped_by"] is None


@pytest.mark.asyncio
async def test_migration_adds_columns_to_a_pre_existing_database(tmp_path: Any) -> None:
    """An installed instance must survive the upgrade with its rules intact."""
    import aiosqlite

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " enabled INTEGER NOT NULL DEFAULT 1, match_source TEXT NOT NULL DEFAULT '',"
            " match_importance TEXT NOT NULL DEFAULT '', target_kind TEXT NOT NULL DEFAULT 'feishu',"
            " target_url TEXT NOT NULL)"
        )
        await db.execute("INSERT INTO rules (name, target_url) VALUES ('pre-existing', 'http://kept')")
        await db.commit()

    store = Store(path)
    await store.open()
    try:
        rules = await store.active_rules()
        assert [r["name"] for r in rules] == ["pre-existing"]
        assert rules[0]["priority"] == 0 and rules[0]["stop_on_match"] == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cooldown_is_scoped_per_rule_not_shared_across_targets(store: Store) -> None:
    """A chatty on-call rule must not silence an archive sink sharing the alert."""
    await store.add_rule({"name": "oncall", "target_kind": "generic", "target_url": "http://oncall"})
    first = await _run(store, ALERT, dedup_window_seconds=0)
    assert first["rules"] == ["oncall"]

    # archive is added afterwards, so it has no delivery history of its own.
    await store.add_rule({"name": "archive", "target_kind": "generic", "target_url": "http://archive"})
    second = await _run(store, ALERT, dedup_window_seconds=0, cooldown_seconds=3600)

    # oncall is cooling; archive has its own clock and must still be delivered.
    assert second["outcome"] == "forwarded"
    assert second["rules"] == ["archive"]

    step = next(s for s in (await store.list_decisions())[0]["steps"] if s["step"] == "cooldown")
    assert step["cooled"] == ["oncall"]


@pytest.mark.asyncio
async def test_alert_is_skipped_only_when_every_matching_rule_is_cooling(store: Store) -> None:
    await store.add_rule({"name": "oncall", "target_kind": "generic", "target_url": "http://oncall"})
    await _run(store, ALERT, dedup_window_seconds=0)
    result = await _run(store, ALERT, dedup_window_seconds=0, cooldown_seconds=3600)

    assert result["skip_code"] == "cooldown"
    step = next(s for s in (await store.list_decisions())[0]["steps"] if s["step"] == "cooldown")
    assert step["cooled"] == ["oncall"]


@pytest.mark.asyncio
async def test_cooldown_zero_disables_the_gate(store: Store) -> None:
    await store.add_rule({"name": "oncall", "target_kind": "generic", "target_url": "http://oncall"})
    await _run(store, ALERT, dedup_window_seconds=0)
    result = await _run(store, ALERT, dedup_window_seconds=0, cooldown_seconds=0)

    assert result["outcome"] == "forwarded"


def test_dashboard_does_not_poll_aggressively() -> None:
    """The console must not hammer the API, and must be pausable.

    A 5s poll was 1440 requests/hour per open tab for a stream where alerts
    arrive minutes apart. The last-updated stamp is part of the contract: once
    refreshing can be turned off, a paused dashboard and a quiet one look
    identical without it.
    """
    from lite.dashboard import DASHBOARD_HTML as html

    assert "DEFAULT_INTERVAL = 30" in html
    assert 'id="interval"' in html and 'id="refresh"' in html
    assert 'id="updated"' in html
    assert "visibilitychange" in html  # a hidden tab must stop polling
    assert "setInterval(refresh, 5000)" not in html


def test_recovery_card_cannot_be_mistaken_for_the_alert_it_closes() -> None:
    """Real traffic produced two identical-looking Lark cards, one a recovery."""
    from lite import channels

    base = {
        "source": "grafana",
        "title": "示例充值超限告警",
        "body": "...",
        "importance": "medium",
        "summary": "用户436293单次充值达500美元",
        "route": "ai",
    }
    firing = channels.build_payload("feishu", {**base, "resolved": False})
    recovered = channels.build_payload("feishu", {**base, "resolved": True})

    assert firing["card"]["header"]["template"] != recovered["card"]["header"]["template"]
    assert recovered["card"]["header"]["template"] == "green"
    assert recovered["card"]["header"]["title"]["content"].startswith("[RESOLVED]")
    assert "**Status** RESOLVED" in recovered["card"]["elements"][0]["text"]["content"]
    assert "**Status** FIRING" in firing["card"]["elements"][0]["text"]["content"]

    # Machine consumers need the same distinction.
    assert channels.build_payload("generic", {**base, "resolved": True})["status"] == "resolved"
    assert channels.build_payload("generic", {**base, "resolved": False})["status"] == "firing"


def test_recovery_keeps_the_importance_that_routes_it_to_the_same_people() -> None:
    """Downgrading recoveries would page someone and never tell them it ended."""
    from lite.triage import rule_triage

    firing = rule_triage("prod", "service down", "critical outage", resolved=False)
    recovered = rule_triage("prod", "service down", "critical outage", resolved=True)

    assert firing["importance"] == recovered["importance"] == "high"
