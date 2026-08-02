"""The runtime settings plane: live overrides for operator-policy config.

Pays down the dual-config-plane debt. The keys registered here (the
`[runtime-policy]` tags in .env.example.all) can be overridden at runtime via
the admin API and take effect across all processes within seconds — no file
edit, no restart. Resolution order everywhere is:

    DB override (this plane)  >  env value  >  code default

Design constraints that shaped this module:

- POLICY READERS ARE SYNC. Every consumer reads through a `*Policy.from_config`
  choke point that is synchronous, so overrides are served from an in-memory
  SNAPSHOT dict swapped atomically by an async refresher (startup + every
  `_REFRESH_INTERVAL_SECONDS` + on a Redis pub/sub nudge after writes). Reads
  never await and never touch the DB.
- FAIL-OPEN. A DB or Redis problem keeps the last snapshot (or falls back to
  env entirely); the hot path must never depend on this plane being healthy.
- VALIDATE ON WRITE. Values are stored as strings; the registry below is the
  single source of what exists and what parses. The API rejects anything the
  registry cannot cast, so readers may trust stored values (and still fall
  back if one slips through).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from models import RuntimeSetting

logger = get_logger("operations.runtime_settings")

_CHANNEL = "webhookwise:runtime_settings:invalidate"
_REFRESH_INTERVAL_SECONDS = 60.0
_REFRESH_ERRORS = (Exception,)  # refresher must survive anything; it logs and keeps the old snapshot


def _cast_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise ValueError("expected true/false")


def _cast_int(minimum: int | None = None, maximum: int | None = None) -> Callable[[str], int]:
    def cast(raw: str) -> int:
        value = int(raw.strip())
        if minimum is not None and value < minimum:
            raise ValueError(f"must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"must be <= {maximum}")
        return value

    return cast


def _cast_float(minimum: float | None = None, maximum: float | None = None) -> Callable[[str], float]:
    def cast(raw: str) -> float:
        value = float(raw.strip())
        if minimum is not None and value < minimum:
            raise ValueError(f"must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"must be <= {maximum}")
        return value

    return cast


def _cast_importance_mapping(raw: str) -> str:
    """Strict on write (the runtime parser is lenient and drops bad entries)."""
    from services.incidents.auto_sla import parse_importance_minutes

    text = raw.strip()
    if not text:
        return ""
    entries = [part.strip() for part in text.split(",") if part.strip()]
    parsed = parse_importance_minutes(text)
    if len(parsed) != len(entries):
        raise ValueError('expected entries like "high=30,medium=240" with levels high/medium/low')
    return text


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    domain: str
    cast: Callable[[str], Any]
    description: str


_SPEC_LIST: tuple[SettingSpec, ...] = (
    # Flapping
    SettingSpec("FLAPPING_WINDOW_MINUTES", "flapping", _cast_int(1, 1440), "Flip-counting window (minutes)"),
    SettingSpec(
        "FLAPPING_MIN_TRANSITIONS",
        "flapping",
        _cast_int(1, 1000),
        "Flips within the window that mark an identity flapping",
    ),
    SettingSpec(
        "FLAPPING_SUPPRESS_ENABLED", "flapping", _cast_bool, "Withhold notifications while an identity flaps (opt-in)"
    ),
    # Escalation
    SettingSpec(
        "INCIDENT_AUTO_SLA_MINUTES",
        "escalation",
        _cast_importance_mapping,
        'Auto-arm incident SLAs, e.g. "high=30,medium=240"; empty = off',
    ),
    SettingSpec("SLA_BREACH_MENTION_ALL", "escalation", _cast_bool, "@all mention on SLA-breach cards"),
    # Backpressure / queue
    SettingSpec(
        "WEBHOOK_MQ_BACKLOG_WARN_FRACTION",
        "backpressure",
        _cast_float(0.0, 1.0),
        "Backlog fraction that flags the queue in the Action Center; 0 disables",
    ),
    SettingSpec(
        "WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION",
        "backpressure",
        _cast_float(0.0, 1.0),
        "Backlog fraction above which ingress returns 503; 0 disables",
    ),
    SettingSpec(
        "WEBHOOK_INGRESS_STORM_THRESHOLD",
        "backpressure",
        _cast_int(0),
        "Per-alert storm suppression threshold; 0 disables",
    ),
    SettingSpec(
        "WEBHOOK_INGRESS_STORM_WINDOW_SECONDS",
        "backpressure",
        _cast_int(1),
        "Per-alert storm counting window (seconds)",
    ),
    # KB in cards
    SettingSpec(
        "KB_CARD_LINKS_ENABLED", "kb", _cast_bool, "Attach matching published KB entries to Feishu alert cards"
    ),
    SettingSpec("KB_CARD_LINKS_MAX", "kb", _cast_int(1, 5), "Max KB entries per card"),
    # Noise reduction
    SettingSpec("ENABLE_ALERT_NOISE_REDUCTION", "noise", _cast_bool, "Smart noise reduction master switch"),
    SettingSpec("NOISE_REDUCTION_WINDOW_MINUTES", "noise", _cast_int(1, 1440), "Correlation window (minutes)"),
    SettingSpec("ROOT_CAUSE_MIN_CONFIDENCE", "noise", _cast_float(0.0, 1.0), "Min confidence to mark a root cause"),
    SettingSpec(
        "NOISE_RELATED_MIN_CONFIDENCE", "noise", _cast_float(0.0, 1.0), "Min confidence to correlate two alerts"
    ),
    SettingSpec("NOISE_SOURCE_WEIGHT", "noise", _cast_float(0.0, 1.0), "Scoring weight: source similarity"),
    SettingSpec("NOISE_RESOURCE_WEIGHT", "noise", _cast_float(0.0, 1.0), "Scoring weight: resource similarity"),
    SettingSpec("NOISE_SEMANTIC_WEIGHT", "noise", _cast_float(0.0, 1.0), "Scoring weight: semantic similarity"),
    SettingSpec("NOISE_SEVERITY_WEIGHT", "noise", _cast_float(0.0, 1.0), "Scoring weight: severity similarity"),
    SettingSpec("NOISE_TIME_WEIGHT", "noise", _cast_float(0.0, 1.0), "Scoring weight: time proximity"),
    SettingSpec(
        "NOISE_SEVERITY_DOWNGRADE_SCORE",
        "noise",
        _cast_float(0.0, 1.0),
        "Severity downgrade threshold for derived alerts",
    ),
    SettingSpec("SUPPRESS_DERIVED_ALERT_FORWARD", "noise", _cast_bool, "Forward only root-cause alerts"),
    # Notification cadence
    SettingSpec(
        "NOTIFICATION_COOLDOWN_SECONDS", "cadence", _cast_int(0, 86400), "Re-notify cooldown after a delivery (seconds)"
    ),
    SettingSpec("ENABLE_PERIODIC_REMINDER", "cadence", _cast_bool, "Re-notify persisting duplicates on a schedule"),
    SettingSpec("REMINDER_INTERVAL_HOURS", "cadence", _cast_int(1, 720), "Periodic reminder interval (hours)"),
    # Retention
    SettingSpec("DECISION_TRACE_RETENTION_DAYS", "retention", _cast_int(1, 3650), "Days to keep decision-trace rows"),
)

SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in _SPEC_LIST}

# ── Snapshot (sync reads) ─────────────────────────────────────────────────────

_snapshot: dict[str, str] = {}
_refresher_task: asyncio.Task[None] | None = None
_listener_task: asyncio.Task[None] | None = None


def get_override(key: str) -> str | None:
    """Raw override string for `key`, or None. Sync, snapshot-backed."""
    return _snapshot.get(key)


def override_or[T](key: str, fallback: T) -> T:
    """Typed effective value: cast override via the registry, else fallback.

    A stored value that no longer casts (registry tightened after write) falls
    back rather than raising — the hot path never breaks on this plane.
    """
    raw = _snapshot.get(key)
    if raw is None:
        return fallback
    spec = SPECS.get(key)
    if spec is None:
        return fallback
    try:
        return spec.cast(raw)  # type: ignore[no-any-return]
    except (TypeError, ValueError) as e:
        logger.warning("[RuntimeSettings] Stored override for %s no longer parses (%s); using fallback", key, e)
        return fallback


def _swap_snapshot(rows: dict[str, str]) -> None:
    global _snapshot
    _snapshot = rows


def _db_ready() -> bool:
    """Whether the process already has an initialized DB plane.

    The refresher must never be the thing that CREATES the engine: in processes
    (and tests) that never touch the DB, lazily building an engine per refresh
    would leak pools and churn connections for a plane that has nothing to read
    yet. Startup order in real processes initializes the DB first; until then we
    skip and let the interval loop pick overrides up after init.
    """
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    return context is not None and getattr(context, "session_factory", None) is not None


async def refresh_runtime_settings() -> int:
    """Reload all overrides from the DB into the snapshot. Fail-open."""
    from db.session import session_scope

    if not _db_ready():
        return -1
    try:
        async with session_scope() as session:
            rows = (await session.execute(select(RuntimeSetting.key, RuntimeSetting.value))).all()
        _swap_snapshot({str(key): str(value) for key, value in rows})
        return len(rows)
    except _REFRESH_ERRORS as e:  # noqa: BLE001 - keep last snapshot on any failure
        logger.warning("[RuntimeSettings] Refresh failed (keeping previous snapshot): %s", e)
        return -1


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
        await refresh_runtime_settings()


async def _listen_loop() -> None:
    from core import redis_client

    while True:
        try:
            client = redis_client.get_redis()
            pubsub = client.pubsub()
            await pubsub.subscribe(_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    await refresh_runtime_settings()
        except asyncio.CancelledError:
            raise
        except (AttributeError, TypeError) as e:
            # A structurally wrong client (e.g. a test double) will never start
            # working — exit and leave propagation to the interval refresh
            # instead of retrying forever.
            logger.info("[RuntimeSettings] Pub/sub unavailable (%s); relying on interval refresh", e)
            return
        except _REFRESH_ERRORS as e:  # noqa: BLE001 - reconnect after backoff; interval refresh covers the gap
            logger.warning("[RuntimeSettings] Invalidation listener error (retrying in 10s): %s", e)
            await asyncio.sleep(10)


def _alive_on_current_loop(task: asyncio.Task[None] | None) -> TypeGuard[asyncio.Task[None]]:
    """True only for a live task that belongs to the RUNNING loop.

    A pending task left over from an already-closed loop (tests create a loop
    per case) must not satisfy the idempotency check — it will never run again,
    and keeping it would both block a fresh start and pin its object graph.
    """
    if task is None or task.done():
        return False
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


async def start_runtime_settings_plane() -> None:
    """Initial load + background refresher + pub/sub listener (idempotent)."""
    global _refresher_task, _listener_task
    await refresh_runtime_settings()
    if not _alive_on_current_loop(_refresher_task):
        _refresher_task = asyncio.create_task(_refresh_loop(), name="runtime-settings-refresh")
    if not _alive_on_current_loop(_listener_task):
        _listener_task = asyncio.create_task(_listen_loop(), name="runtime-settings-listener")


async def stop_runtime_settings_plane() -> None:
    global _refresher_task, _listener_task
    for task in (_refresher_task, _listener_task):
        if not _alive_on_current_loop(task):
            # Stale handle from a closed loop: even .cancel() would raise on a
            # closed loop — just drop the reference and let GC take the graph.
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _refresher_task = None
    _listener_task = None


def _reset_snapshot_for_tests() -> None:
    _swap_snapshot({})


# ── Writes (admin API) ────────────────────────────────────────────────────────


async def set_override(session: AsyncSession, key: str, value: str, *, actor: str = "") -> RuntimeSetting:
    """Validate and upsert one override. Raises ValueError on unknown/invalid."""
    spec = SPECS.get(key)
    if spec is None:
        raise ValueError(f"unknown runtime setting {key!r}")
    spec.cast(str(value))  # raises ValueError with a human message
    row = await session.get(RuntimeSetting, key)
    if row is None:
        row = RuntimeSetting(key=key, value=str(value), updated_by=actor, updated_at=utcnow())
        session.add(row)
    else:
        row.value = str(value)
        row.updated_by = actor
        row.updated_at = utcnow()
    await session.flush()
    return row


async def clear_override(session: AsyncSession, key: str) -> bool:
    if key not in SPECS:
        raise ValueError(f"unknown runtime setting {key!r}")
    row = await session.get(RuntimeSetting, key)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def publish_runtime_settings_invalidation() -> None:
    """Nudge every process to refresh; local snapshot refreshes immediately."""
    await refresh_runtime_settings()
    try:
        from core import redis_client

        await redis_client.redis_publish(_CHANNEL, "invalidate")
    except _REFRESH_ERRORS as e:  # noqa: BLE001 - peers fall back to the interval refresh
        logger.warning(
            "[RuntimeSettings] Invalidation publish failed (peers refresh within %ss): %s",
            int(_REFRESH_INTERVAL_SECONDS),
            e,
        )
