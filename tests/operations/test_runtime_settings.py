"""Runtime settings plane: resolution, validation, refresh, policy integration."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.operations import runtime_settings as rs


@pytest.fixture(autouse=True)
def _clean_snapshot():
    rs._reset_snapshot_for_tests()
    yield
    rs._reset_snapshot_for_tests()


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_session_factory.begin() as sess:
        yield sess


def test_override_or_resolution_order() -> None:
    # No override → fallback (the env/default value passed by the caller).
    assert rs.override_or("FLAPPING_MIN_TRANSITIONS", 6) == 6
    # Override present → cast wins over fallback.
    rs._swap_snapshot({"FLAPPING_MIN_TRANSITIONS": "9"})
    assert rs.override_or("FLAPPING_MIN_TRANSITIONS", 6) == 9
    # A stored value that no longer parses falls back instead of raising.
    rs._swap_snapshot({"FLAPPING_MIN_TRANSITIONS": "not-a-number"})
    assert rs.override_or("FLAPPING_MIN_TRANSITIONS", 6) == 6
    # Unregistered keys always fall back.
    rs._swap_snapshot({"NOT_A_SETTING": "1"})
    assert rs.override_or("NOT_A_SETTING", "x") == "x"


def test_registry_covers_only_real_config_fields() -> None:
    """Every registered key must exist as a config model field — the registry
    must never drift from core/config/defaults.py."""
    from pathlib import Path

    defaults = (Path(__file__).resolve().parents[2] / "core/config/defaults.py").read_text()
    missing = [key for key in rs.SPECS if f"    {key}:" not in defaults]
    assert missing == []


def test_env_reference_tags_match_the_registry_exactly() -> None:
    """`[runtime-policy]` in .env.example.all promises "live-editable".

    README and the env legend both state that every tagged key is editable
    through this plane, so the tags and the registry must agree in BOTH
    directions — a tagged-but-unregistered key is a documented lie, and a
    registered-but-untagged key is an operator knob nobody can discover.
    """
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[2] / ".env.example.all").read_text().splitlines()
    tagged = {lines[i + 1].split("=")[0] for i, line in enumerate(lines) if line.startswith("# [runtime-policy]")}

    assert sorted(tagged - set(rs.SPECS)) == [], "tagged in env reference but not registered"
    assert sorted(set(rs.SPECS) - tagged) == [], "registered but not tagged in env reference"


def test_write_validation_is_strict() -> None:
    # Cast-level checks (no session needed).
    with pytest.raises(ValueError):
        rs.SPECS["FLAPPING_SUPPRESS_ENABLED"].cast("maybe")
    with pytest.raises(ValueError):
        rs.SPECS["KB_CARD_LINKS_MAX"].cast("99")  # above max 5
    with pytest.raises(ValueError):
        rs.SPECS["NOISE_SOURCE_WEIGHT"].cast("1.5")  # above 1.0
    with pytest.raises(ValueError):
        rs.SPECS["INCIDENT_AUTO_SLA_MINUTES"].cast("critical=5")  # invalid level
    assert rs.SPECS["INCIDENT_AUTO_SLA_MINUTES"].cast("high=30,medium=240") == "high=30,medium=240"
    assert rs.SPECS["INCIDENT_AUTO_SLA_MINUTES"].cast("") == ""
    assert rs.SPECS["FLAPPING_SUPPRESS_ENABLED"].cast("TRUE") is True


@pytest.mark.asyncio
async def test_set_clear_and_refresh_round_trip(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = db_app_context_session_factory
    async with factory.begin() as session:
        await rs.set_override(session, "FLAPPING_SUPPRESS_ENABLED", "true", actor="ops-a")

    # The snapshot is only updated by refresh — prove the full DB round trip.
    assert rs.get_override("FLAPPING_SUPPRESS_ENABLED") is None
    count = await rs.refresh_runtime_settings()
    assert count == 1
    assert rs.get_override("FLAPPING_SUPPRESS_ENABLED") == "true"
    assert rs.override_or("FLAPPING_SUPPRESS_ENABLED", False) is True

    async with factory.begin() as session:
        assert await rs.clear_override(session, "FLAPPING_SUPPRESS_ENABLED") is True
    await rs.refresh_runtime_settings()
    assert rs.get_override("FLAPPING_SUPPRESS_ENABLED") is None
    async with factory.begin() as session:
        # Clearing an absent override reports False rather than erroring.
        assert await rs.clear_override(session, "FLAPPING_SUPPRESS_ENABLED") is False


@pytest.mark.asyncio
async def test_set_override_rejects_invalid_value(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="must be <= 5"):
        await rs.set_override(session, "KB_CARD_LINKS_MAX", "50")
    with pytest.raises(ValueError, match="unknown runtime setting"):
        await rs.set_override(session, "TOTALLY_UNKNOWN", "1")


def test_policies_consume_overrides() -> None:
    """The choke points actually read the snapshot — the debt is really paid."""
    from services.incidents.auto_sla import AutoSlaPolicy
    from services.webhooks.decisioning import forwarding_policy_from_config
    from services.webhooks.flapping import FlappingPolicy

    baseline = FlappingPolicy.from_config()
    assert baseline.suppress_enabled is False  # env default

    rs._swap_snapshot(
        {
            "FLAPPING_SUPPRESS_ENABLED": "true",
            "FLAPPING_MIN_TRANSITIONS": "3",
            "INCIDENT_AUTO_SLA_MINUTES": "high=15",
            "NOTIFICATION_COOLDOWN_SECONDS": "120",
        }
    )
    flapping = FlappingPolicy.from_config()
    assert flapping.suppress_enabled is True
    assert flapping.min_transitions == 3

    auto_sla = AutoSlaPolicy.from_config()
    assert auto_sla.minutes_by_importance == {"high": 15}

    forwarding = forwarding_policy_from_config()
    assert forwarding.notification_cooldown_seconds == 120


def test_ai_spend_policy_is_tunable_without_an_ssh() -> None:
    """The last operator decisions that lived only in .env.

    Invisible from the dashboard, needing a file edit and a restart to change,
    and — for the exclusion list — failing silently on a typo. Registering them
    here is what makes them discoverable; the readers go through override_or so
    a stored value actually wins.
    """
    from services.operations import runtime_settings as rs

    ai_keys = {key for key, spec in rs.SPECS.items() if spec.domain == "ai"}
    assert ai_keys == {
        "AI_EXCLUDED_RULES",
        "AI_ROUTING_ENABLED",
        "AI_ROUTING_SKIP_IMPORTANCE",
        "AI_COST_MONTHLY_BUDGET_USD",
        "AI_COST_BUDGET_ENFORCE",
    }
    # Credentials and endpoints are NOT tunable here: they describe the
    # deployment, not the policy, and a live-editable API key is a liability.
    assert not any(key.startswith(("OPENAI_", "AI_BASE", "AI_MODEL")) for key in rs.SPECS)


def test_an_exclusion_list_rejects_the_shapes_that_would_silently_do_nothing() -> None:
    from services.operations import runtime_settings as rs

    cast = rs.SPECS["AI_EXCLUDED_RULES"].cast
    assert cast(" 示例充值超限告警 , 示例提现超限告警 ") == "示例充值超限告警,示例提现超限告警"
    assert cast("") == ""
    with pytest.raises(ValueError):
        cast("充值报警,,提现报警")  # a stray comma silently excludes nothing
    with pytest.raises(ValueError):
        cast("x" * 201)


def test_the_skip_list_only_accepts_real_importances() -> None:
    from services.operations import runtime_settings as rs

    cast = rs.SPECS["AI_ROUTING_SKIP_IMPORTANCE"].cast
    assert cast("Low, Medium") == "low,medium"
    with pytest.raises(ValueError):
        cast("low,urgent")
