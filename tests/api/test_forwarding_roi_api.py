"""Forward-rule ROI enrichment on the list endpoint (direct calls, sqlite)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def session(db_session):
    return db_session


@pytest.mark.asyncio
async def test_list_forward_rules_annotates_hit_counts(session: AsyncSession) -> None:
    from api.v1 import forwarding as api
    from models import DecisionTrace, ForwardRule

    # Two rules: one that has matched alerts, one enabled "zombie" that hasn't.
    busy = ForwardRule(name="busy-rule", target_type="feishu", target_url="https://example.com/hook/x", enabled=True)
    zombie = ForwardRule(
        name="zombie-rule", target_type="feishu", target_url="https://example.com/hook/y", enabled=True
    )
    session.add_all([busy, zombie])
    # Two forwarded traces that matched busy-rule.
    session.add_all(
        [
            DecisionTrace(webhook_event_id=1, outcome="forwarded", skip_code="none", matched_rules=["busy-rule"]),
            DecisionTrace(webhook_event_id=2, outcome="forwarded", skip_code="none", matched_rules=["busy-rule"]),
        ]
    )
    await session.commit()

    result = await api.get_forward_rules_endpoint(session=session)
    by_name = {r["name"]: r for r in result["data"]}
    assert by_name["busy-rule"]["hit_count"] == 2
    assert by_name["busy-rule"]["last_matched_at"] is not None
    # The zombie rule reports zero, no last-matched timestamp.
    assert by_name["zombie-rule"]["hit_count"] == 0
    assert by_name["zombie-rule"]["last_matched_at"] is None
    # Masked list must not leak the raw target URL secret.
    assert "example.com/hook/x" not in str(by_name["busy-rule"]["target_url"])


@pytest.mark.asyncio
async def test_a_deep_analysis_rule_says_which_gateway_it_reaches(session: AsyncSession) -> None:
    """A deep-analysis rule has no target_url: the gateway is server config, so
    one setting repoints every such rule at once.

    The cost of that design was a rule card reading "deep analysis (deep
    analysis)" above an empty address — accurate and useless, and actively
    misleading once more than one gateway product exists. The list endpoint now
    answers "which one", per rule, from configuration.
    """
    from api.v1 import forwarding as api
    from core.app_context import get_config_manager
    from models import ForwardRule

    config = get_config_manager()
    config.deep_analysis.DEEP_ANALYSIS_PLATFORM = "hookprobe"
    config.deep_analysis.DEEP_ANALYSIS_GATEWAY_URL = "http://hookprobe:8088"
    config.deep_analysis.DEEP_ANALYSIS_ENABLED = True

    session.add_all(
        [
            ForwardRule(name="deep-rule", target_type="deep_analysis", target_url="", enabled=True),
            ForwardRule(name="card-rule", target_type="feishu", target_url="https://example.com/hook/z", enabled=True),
        ]
    )
    await session.commit()

    by_name = {r["name"]: r for r in (await api.get_forward_rules_endpoint(session=session))["data"]}

    target = by_name["deep-rule"]["deep_analysis_target"]
    assert target["platform"] == "hookprobe"
    assert "hookprobe:8088" in target["gateway_url"]
    assert target["enabled"] is True
    # Only deep-analysis rules get it; a feishu rule already shows its own URL.
    assert by_name["card-rule"].get("deep_analysis_target") is None


@pytest.mark.asyncio
async def test_the_gateway_platform_follows_configuration(session: AsyncSession) -> None:
    """Swapping the gateway is one setting, so the card must follow it rather
    than hard-code a product — the whole reason the layer was made neutral."""
    from api.v1 import forwarding as api
    from core.app_context import get_config_manager
    from models import ForwardRule

    session.add(ForwardRule(name="deep-rule", target_type="deep_analysis", target_url="", enabled=True))
    await session.commit()

    config = get_config_manager()
    for platform in ("openclaw", "hermes", "hookprobe"):
        config.deep_analysis.DEEP_ANALYSIS_PLATFORM = platform
        rules = (await api.get_forward_rules_endpoint(session=session))["data"]
        assert rules[0]["deep_analysis_target"]["platform"] == platform
