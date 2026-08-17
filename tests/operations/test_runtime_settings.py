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
    assert {
        "AI_EXCLUDED_RULES",
        "AI_ROUTING_ENABLED",
        "AI_ROUTING_SKIP_IMPORTANCE",
        "AI_COST_MONTHLY_BUDGET_USD",
        "AI_COST_BUDGET_ENFORCE",
    } <= ai_keys
    # Credentials and endpoints are NOT tunable here: they describe the
    # deployment, not the policy, and a live-editable API key is a liability.
    # Named rather than prefix-matched — OPENAI_TEMPERATURE is policy and does
    # belong, so a prefix rule would either exclude it or protect nothing.
    forbidden = {
        "OPENAI_API_KEY",
        "OPENAI_API_URL",
        "DEEP_ANALYSIS_GATEWAY_TOKEN",
        "DEEP_ANALYSIS_HOOKS_TOKEN",
        "DEEP_ANALYSIS_GATEWAY_URL",
        "DEEP_ANALYSIS_HTTP_API_URL",
        "DEEP_ANALYSIS_DEVICE_PRIVATE_KEY_PEM",
        "DEEP_ANALYSIS_DEVICE_TOKEN",
        "API_KEY",
        "ADMIN_WRITE_KEY",
        "DATABASE_URL",
        "REDIS_URL",
    }
    assert forbidden.isdisjoint(rs.SPECS)


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


def test_every_registered_key_is_actually_read_through_the_plane() -> None:
    """Registering a key without reading it produces a switch that does nothing.

    The dashboard would show it, an operator would change it, the value would be
    stored — and the code would keep reading the environment. That is the exact
    failure shape this session kept hitting, so it gets a test rather than a
    convention.
    """
    import re
    from pathlib import Path

    from services.operations import runtime_settings as rs

    root = Path(__file__).resolve().parents[2]
    sources = []
    for folder in ("services", "core", "api"):
        sources.extend((root / folder).rglob("*.py"))
    read = set()
    # Digits included: AI_COST_PER_1K_INPUT_TOKENS was missed by [A-Z_]+ alone.
    pattern = re.compile(r'override_or\(\s*"([A-Z0-9_]+)"')
    for path in sources:
        if path.name == "runtime_settings.py":
            continue
        read.update(pattern.findall(path.read_text(encoding="utf-8")))
    # The helper in operations/policies.py wraps override_or under its own name.
    wrapper = re.compile(r'_rt_override\(\s*"([A-Z0-9_]+)"')
    # And a module that resolves keys by attribute name declares them in a
    # RUNTIME_KEYS tuple, because a dynamic lookup is exactly where a dead
    # setting would hide from this check.
    declared = re.compile(r"RUNTIME_KEYS\s*=\s*\(([^)]*)\)", re.S)
    literal = re.compile(r'"([A-Z0-9_]+)"')
    for path in sources:
        text = path.read_text(encoding="utf-8")
        read.update(wrapper.findall(text))
        for block in declared.findall(text):
            read.update(literal.findall(block))

    unread = sorted(set(rs.SPECS) - read)
    assert unread == [], f"registered but never read through the plane: {unread}"


def test_every_registered_key_resolves_an_env_default(temp_config) -> None:
    """The dashboard's env-default column resolves each registered key against
    the config groups. The old hand-maintained map silently fell behind the
    registry — 53 of 80 keys showed a blank default, including one added the
    very day it shipped — so this asserts EVERY key resolves for real."""
    from api.v1.runtime_settings import _env_value
    from services.operations import runtime_settings as rs

    unresolved = sorted(key for key in rs.SPECS if _env_value(key) is None)
    assert unresolved == [], f"registered keys with no resolvable env default: {unresolved}"
