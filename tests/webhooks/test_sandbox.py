"""Tests for the webhook payload test sandbox (dry-run, zero side effects)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models import ForwardRule, Silence
from services.webhooks import sandbox


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _volcengine_payload() -> dict[str, object]:
    return {
        "Type": "alarm",
        "RuleName": "GPU memory high",
        "Level": "critical",
        "ProjectName": "eve-cn-prod",
        "Region": "cn-beijing",
        "Resources": [{"ResourceName": "gpu-node-7", "Metrics": [{"MetricName": "gpu_mem_used", "Value": 98}]}],
    }


@pytest.fixture(autouse=True)
def _reset_caches() -> AsyncIterator[None]:
    # The rules/silences loaders cache per-process; clear so each test sees only
    # the rows it seeded into its own sqlite session.
    from services.forwarding.rules import invalidate_forward_rules_cache
    from services.silences.store import invalidate_silences_cache

    invalidate_forward_rules_cache()
    invalidate_silences_cache()
    yield
    invalidate_forward_rules_cache()
    invalidate_silences_cache()


@pytest.mark.asyncio
async def test_sandbox_parses_and_fingerprints(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        result = await sandbox.test_webhook_payload(session, source="volcengine", payload=_volcengine_payload())

    # Source resolved through the volcengine adapter (not passthrough).
    assert result["source"]["resolved"] == "volcengine"
    assert result["source"]["matched"] is True
    assert result["source"]["adapter"] != "passthrough"
    # Deterministic fingerprints present (pure sha256).
    assert len(result["alert_hash"]) == 64
    assert len(result["dedup_key"]) == 64
    # Identity extracted (project/region surfaced for routing).
    assert result["match_fields"]["project"]
    assert result["match_fields"]["region"]
    # Rule-based analysis is clearly labelled as the non-AI fallback.
    assert "AI" in result["rule_based_analysis"]["note"]
    assert result["rule_based_analysis"]["importance"] in {"high", "medium", "low", "unknown"}


@pytest.mark.asyncio
async def test_sandbox_passthrough_for_unknown_source(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        result = await sandbox.test_webhook_payload(session, source="totally-made-up", payload={"foo": "bar"})
    # An unrecognized source with a non-matching payload falls back to passthrough.
    assert result["source"]["adapter"] == "passthrough"
    assert result["source"]["matched"] is False


@pytest.mark.asyncio
async def test_sandbox_reports_matched_forward_rule(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # A rule that forwards volcengine alerts should show up as matched, and the
    # dry-run should say it would forward — without enqueuing anything.
    async with session_factory.begin() as session:
        session.add(
            ForwardRule(
                name="volc->ops",
                match_source="volcengine",
                target_type="feishu",
                target_name="ops-group",
                target_url="https://example.com/hook/secret",
                enabled=True,
                priority=10,
            )
        )

    async with session_factory() as session:
        result = await sandbox.test_webhook_payload(session, source="volcengine", payload=_volcengine_payload())

    fwd = result["forwarding"]
    assert fwd["should_forward"] is True
    assert fwd["skip_code"] == "none"
    names = [r["name"] for r in fwd["matched_rules"]]
    assert "volc->ops" in names
    # The secret target URL is never echoed back in the rule summary.
    assert all("target_url" not in r for r in fwd["matched_rules"])


@pytest.mark.asyncio
async def test_sandbox_reports_silence_match(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # An active silence matching the source mutes forwarding; the dry-run reports
    # which silence won.
    async with session_factory.begin() as session:
        silence = Silence(match_source="volcengine", comment="muted")
        session.add(silence)
        # Also a rule, to prove silence wins over a matching rule.
        session.add(
            ForwardRule(
                name="volc->ops",
                match_source="volcengine",
                target_type="feishu",
                target_url="https://example.com/hook/secret",
                enabled=True,
                priority=10,
            )
        )

    async with session_factory() as session:
        # fetch the silence id back
        result = await sandbox.test_webhook_payload(session, source="volcengine", payload=_volcengine_payload())

    fwd = result["forwarding"]
    assert fwd["should_forward"] is False
    assert fwd["skip_code"] == "silenced"
    assert fwd["silenced_by"] is not None
    assert fwd["silenced_by"]["silence_id"] is not None


@pytest.mark.asyncio
async def test_sandbox_no_match_skips(session_factory: async_sessionmaker[AsyncSession]) -> None:
    # No rules configured → nothing matches → skip_code no_match, no forward.
    async with session_factory() as session:
        result = await sandbox.test_webhook_payload(session, source="volcengine", payload=_volcengine_payload())
    fwd = result["forwarding"]
    assert fwd["should_forward"] is False
    assert fwd["skip_code"] == "no_match"
    assert fwd["matched_rules"] == []
