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


def _cast_fingerprint_fields(raw: str) -> str:
    """Validate the JSON {source: [dot.paths]} shape without normalizing it."""
    from core import json

    text = raw.strip()
    if not text:
        return ""
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("expected a JSON object mapping source -> field list")
    for source, fields in loaded.items():
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f.strip() for f in fields):
            raise ValueError(f"fields for {source!r} must be a non-empty list of dot-paths")
    return text


def _cast_choice(*choices: str) -> Callable[[str], str]:
    def cast(raw: str) -> str:
        value = raw.strip().lower()
        if value not in choices:
            raise ValueError(f"expected one of: {', '.join(choices)}")
        return value

    return cast


def _cast_csv_names(raw: str) -> str:
    """A comma-separated list of names, normalised and bounded.

    Kept strict on write because the failure this guards is silent: a name that
    does not match any alert rule excludes nothing, and nothing anywhere says
    so. Rejecting the shapes that are obviously wrong — empty entries, a stray
    separator, absurd length — at least turns a slip into an error message.
    """
    names = [part.strip() for part in raw.split(",")]
    if any(not name for name in names) and raw.strip():
        raise ValueError("empty entry in the list (check for a stray comma)")
    cleaned = [name for name in names if name]
    if len(cleaned) > 100:
        raise ValueError("more than 100 entries")
    if any(len(name) > 200 for name in cleaned):
        raise ValueError("an entry is longer than 200 characters")
    return ",".join(cleaned)


def _cast_importance_list(raw: str) -> str:
    allowed = {"critical", "high", "medium", "low"}
    values = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise ValueError(f"not an importance: {', '.join(unknown)}")
    return ",".join(values)


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
    # AI spend policy. These were the last operator decisions still living only
    # in .env: invisible from the dashboard, needing an SSH and a restart, with
    # a typo failing silently. Credentials and endpoints stay in env, where they
    # belong; what is tunable here is policy.
    SettingSpec(
        "AI_EXCLUDED_RULES",
        "ai",
        _cast_csv_names,
        "Alert rules never worth a model call (exact names, comma-separated); also blocks deep analysis",
    ),
    SettingSpec("AI_ROUTING_ENABLED", "ai", _cast_bool, "Let the cheap rule pass answer low-value alerts"),
    SettingSpec(
        "AI_ROUTING_SKIP_IMPORTANCE",
        "ai",
        _cast_importance_list,
        'Importances the rule pass may answer alone, e.g. "low"',
    ),
    SettingSpec(
        "AI_CORRECTION_PRIOR_ENABLED",
        "ai",
        _cast_bool,
        "Tell the model what operators corrected on other instances of the same alert rule (opt-in)",
    ),
    SettingSpec(
        "AI_CORRECTION_PRIOR_MIN_CORRECTIONS",
        "ai",
        _cast_int(1, 100),
        "Agreeing corrections on a rule before the prior is stated at all",
    ),
    SettingSpec(
        "AI_CORRECTION_PRIOR_LOOKBACK_DAYS",
        "ai",
        _cast_int(1, 3650),
        "How far back a correction still counts as current",
    ),
    SettingSpec(
        "AI_COST_MONTHLY_BUDGET_USD", "ai", _cast_float(0.0), "Month-to-date AI spend ceiling in USD; 0 disables"
    ),
    SettingSpec(
        "AI_COST_BUDGET_ENFORCE", "ai", _cast_bool, "At 100% of the budget, degrade to rules instead of spending"
    ),
    SettingSpec(
        "AI_COST_BUDGET_MODE",
        "ai",
        _cast_choice("off", "shadow", "enforce"),
        "Budget brake ladder: off, shadow (record the refusal, spend anyway), enforce",
    ),
    # Keyword rules. These decide severity before any model sees the alert, and
    # a missing keyword class is invisible: the payment-alert downgrade lived in
    # this configuration for weeks because nobody could see it.
    SettingSpec("RULE_HIGH_KEYWORDS", "rules", _cast_csv_names, "Level/name keywords that mean high"),
    SettingSpec(
        "RULE_CONTENT_HIGH_KEYWORDS",
        "rules",
        _cast_csv_names,
        "Content keywords that floor the verdict at high (money, account security)",
    ),
    SettingSpec("RULE_WARN_KEYWORDS", "rules", _cast_csv_names, "Keywords that mean medium"),
    SettingSpec("RULE_METRIC_KEYWORDS", "rules", _cast_csv_names, "Keywords that mark a metric alert"),
    SettingSpec(
        "RULE_THRESHOLD_MULTIPLIER", "rules", _cast_float(0.1, 100.0), "How far past a threshold counts as severe"
    ),
    # Ingest: what counts as the same alert, and how hard to retry one.
    SettingSpec("DEDUP_WINDOW_SECONDS", "ingest", _cast_int(0, 86400), "Repeats within this window join a thread"),
    SettingSpec(
        "DEDUP_FINGERPRINT_MODE",
        "ingest",
        _cast_choice("off", "shadow", "enforce"),
        "Per-source dedup fingerprint ladder: off, shadow (count divergence), enforce",
    ),
    SettingSpec(
        "DEDUP_FINGERPRINT_FIELDS",
        "ingest",
        _cast_fingerprint_fields,
        "JSON {source: [dot.paths]} naming the fields that ARE that source's alert identity",
    ),
    SettingSpec(
        "ANALYSIS_REUSE_WINDOW_SECONDS", "ingest", _cast_int(0, 86400), "How long one analysis answers restatements"
    ),
    SettingSpec("WEBHOOK_RETRY_MAX_RETRIES", "ingest", _cast_int(0, 20), "Attempts before a webhook is dead-lettered"),
    SettingSpec("WEBHOOK_RETRY_INITIAL_DELAY_SECONDS", "ingest", _cast_float(0.0, 600.0), "First retry delay"),
    SettingSpec("WEBHOOK_RETRY_MAX_DELAY_SECONDS", "ingest", _cast_float(0.0, 3600.0), "Retry delay ceiling"),
    SettingSpec("WEBHOOK_RETRY_BACKOFF_MULTIPLIER", "ingest", _cast_float(1.0, 10.0), "Retry backoff multiplier"),
    SettingSpec("AI_PAYLOAD_MAX_BYTES", "ingest", _cast_int(1024, 5_000_000), "Payload bytes kept for analysis"),
    SettingSpec("AI_PAYLOAD_STRIP_KEYS", "ingest", _cast_csv_names, "Payload keys dropped before the model sees them"),
    # Delivery.
    SettingSpec("FORWARD_TIMEOUT_SECONDS", "delivery", _cast_int(1, 300), "Per-delivery HTTP timeout"),
    SettingSpec("FORWARD_RETRY_MAX_RETRIES", "delivery", _cast_int(0, 20), "Delivery attempts before dead"),
    SettingSpec("FORWARD_RETRY_INITIAL_DELAY_SECONDS", "delivery", _cast_float(0.0, 600.0), "First retry delay"),
    SettingSpec("FORWARD_RETRY_MAX_DELAY_SECONDS", "delivery", _cast_float(0.0, 3600.0), "Retry delay ceiling"),
    SettingSpec("FORWARD_RETRY_BACKOFF_MULTIPLIER", "delivery", _cast_float(1.0, 10.0), "Retry backoff multiplier"),
    SettingSpec(
        "FORWARD_MAX_DELIVERY_AGE_SECONDS",
        "delivery",
        _cast_int(0, 604800),
        "Stop delivering a queued alert older than this; 0 = never expire",
    ),
    # Retention: the answers to "can we keep less".
    SettingSpec("ENABLE_DATA_CLEANUP", "retention", _cast_bool, "Run the daily purge at all"),
    SettingSpec("DATA_RETENTION_DAYS_DEFAULT", "retention", _cast_int(1, 3650), "Default event retention (days)"),
    SettingSpec("ARCHIVE_RETENTION_DAYS", "retention", _cast_int(0, 3650), "Archived event retention (days)"),
    SettingSpec("AI_USAGE_RETENTION_DAYS", "retention", _cast_int(0, 3650), "AI usage log retention (days)"),
    SettingSpec(
        "TERMINAL_OUTBOX_RETENTION_DAYS", "retention", _cast_int(0, 3650), "Settled outbox row retention (days)"
    ),
    SettingSpec("INCIDENT_AUTO_CLOSE_DAYS", "retention", _cast_int(0, 3650), "Auto-close quiet incidents after"),
    SettingSpec("MAINTENANCE_HOUR", "retention", _cast_int(0, 23), "Hour of day the purge runs (UTC)"),
    # Deep analysis: the knobs reached for during an incident.
    SettingSpec("DEEP_ANALYSIS_ENABLED", "deep_analysis", _cast_bool, "Trigger investigations at all"),
    SettingSpec("DEEP_ANALYSIS_TIMEOUT_SECONDS", "deep_analysis", _cast_int(30, 3600), "How long to wait for a report"),
    SettingSpec(
        "DEEP_ANALYSIS_POLL_INITIAL_DELAY_SECONDS", "deep_analysis", _cast_float(0.5, 120.0), "First poll delay"
    ),
    SettingSpec("DEEP_ANALYSIS_POLL_MAX_DELAY_SECONDS", "deep_analysis", _cast_float(1.0, 600.0), "Poll delay ceiling"),
    SettingSpec(
        "DEEP_ANALYSIS_POLL_BACKOFF_MULTIPLIER", "deep_analysis", _cast_float(1.0, 10.0), "Poll backoff multiplier"
    ),
    SettingSpec(
        "DEEP_ANALYSIS_MAX_CONSECUTIVE_ERRORS", "deep_analysis", _cast_int(1, 100), "Errors before a run is abandoned"
    ),
    SettingSpec(
        "DEEP_ANALYSIS_ENABLE_DEGRADATION", "deep_analysis", _cast_bool, "Report a failed investigation as a result"
    ),
    # AI, beyond the spend policy already here.
    SettingSpec("ENABLE_AI_ANALYSIS", "ai", _cast_bool, "Call a model at all"),
    SettingSpec("ENABLE_AI_DEGRADATION", "ai", _cast_bool, "Fall back to rules when the model cannot answer"),
    SettingSpec("OPENAI_TEMPERATURE", "ai", _cast_float(0.0, 2.0), "Sampling temperature"),
    SettingSpec("CACHE_ENABLED", "ai", _cast_bool, "Reuse an identical alert's analysis"),
    SettingSpec("ANALYSIS_CACHE_TTL_SECONDS", "ai", _cast_int(0, 604800), "How long a cached analysis answers"),
    SettingSpec("AI_COST_PER_1K_INPUT_TOKENS", "ai", _cast_float(0.0, 1000.0), "Input price used by the cost view"),
    SettingSpec("AI_COST_PER_1K_OUTPUT_TOKENS", "ai", _cast_float(0.0, 1000.0), "Output price used by the cost view"),
    # Breaker thresholds: the knobs an operator reaches for mid-incident. Live
    # because LazyCircuitBreaker rebuilds when these change — registering them
    # before that was true would have shipped four switches that did nothing.
    SettingSpec("CIRCUIT_BREAKER_LLM_THRESHOLD", "breakers", _cast_int(1, 1000), "Failures before the LLM is skipped"),
    SettingSpec(
        "CIRCUIT_BREAKER_LLM_TIMEOUT_SECONDS", "breakers", _cast_float(1.0, 3600.0), "How long the LLM stays skipped"
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_FEISHU_THRESHOLD", "breakers", _cast_int(1, 1000), "Failures before Feishu is skipped"
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_FEISHU_TIMEOUT_SECONDS", "breakers", _cast_float(1.0, 3600.0), "How long Feishu stays skipped"
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_FORWARD_THRESHOLD", "breakers", _cast_int(1, 1000), "Failures before forwarding is skipped"
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_FORWARD_TIMEOUT_SECONDS",
        "breakers",
        _cast_float(1.0, 3600.0),
        "How long forwarding stays skipped",
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_DEEP_ANALYSIS_THRESHOLD",
        "breakers",
        _cast_int(1, 1000),
        "Failures before investigations are skipped",
    ),
    SettingSpec(
        "CIRCUIT_BREAKER_DEEP_ANALYSIS_TIMEOUT_SECONDS",
        "breakers",
        _cast_float(1.0, 3600.0),
        "How long investigations stay skipped",
    ),
    # Escalation
    SettingSpec(
        "INCIDENT_AUTO_SLA_MINUTES",
        "escalation",
        _cast_importance_mapping,
        'Auto-arm incident SLAs, e.g. "high=30,medium=240"; empty = off',
    ),
    SettingSpec("SLA_BREACH_MENTION_ALL", "escalation", _cast_bool, "@all mention on SLA-breach cards"),
    SettingSpec(
        "INCIDENT_RESOLVE_RECAP_ENABLED",
        "escalation",
        _cast_bool,
        "Send one recap card to chat when an incident is resolved",
    ),
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
    SettingSpec(
        "REMEDIATION_VERIFY_DELAY_SECONDS",
        "cadence",
        _cast_int(0, 86400),
        "Seconds after an executed remediation before the target is read back; 0 disables",
    ),
    SettingSpec("REMINDER_INTERVAL_HOURS", "cadence", _cast_int(1, 720), "Periodic reminder interval (hours)"),
    SettingSpec(
        "SELF_NOTIFY_MIN_INTERVAL_MINUTES",
        "cadence",
        _cast_int(1, 1440),
        "Min minutes between out-of-band delivery-failure self-notifications",
    ),
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
