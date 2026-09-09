"""Webhook deduplication — Redis-first sliding window with DB fallback."""

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from redis.exceptions import RedisError

from core import json
from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import REDIS_UNAVAILABLE_TOTAL
from core.redis_client import redis_eval_int, redis_get_json_dict
from core.redis_health import webhook_dedupe
from core.redis_lua import DEDUP_REMEMBER as _DEDUP_REMEMBER_LUA
from db.session import session_scope
from services.analysis.resource_risk import resource_dedup_bucket
from services.webhooks.types import is_analysis_degraded, is_pending_result

if TYPE_CHECKING:
    from services.operations.feature_modes import FeatureMode

logger = get_logger("dedup")

# ── Dedup state (Redis operations) ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DedupState:
    original_event_id: int
    first_seen_at: float
    last_seen_at: float
    count: int
    analysis: dict[str, Any] | None

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds


async def get_dedup_state(dedup_key: str) -> DedupState | None:
    try:
        payload = await redis_get_json_dict(webhook_dedupe(dedup_key))
    except (RedisError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as e:
        REDIS_UNAVAILABLE_TOTAL.labels("dedup", "read_allowed").inc()
        logger.warning(
            "[Dedup] Redis read failed, falling back to DB dedup_key=%s error_type=%s error=%s",
            dedup_key[:32] if dedup_key else "-",
            type(e).__name__,
            e,
            exc_info=True,
        )
        return None
    if not payload:
        return None
    try:
        original_event_id = int(payload.get("original_event_id") or 0)
    except (TypeError, ValueError):
        return None

    analysis = payload.get("analysis")
    return DedupState(
        original_event_id=original_event_id,
        first_seen_at=float(payload.get("first_seen_at") or 0),
        last_seen_at=float(payload.get("last_seen_at") or 0),
        count=int(payload.get("count") or 1),
        analysis=analysis if isinstance(analysis, dict) else None,
    )


async def remember_dedup_state(
    dedup_key: str,
    original_event_id: int,
    analysis: dict[str, Any] | None,
    ttl_seconds: int,
    *,
    reset_chain: bool = False,
) -> None:
    current_time = time.time()
    analysis_json = json.dumps(analysis) if analysis else ""
    try:
        # Atomic read-modify-write in a single round-trip: the script reads the
        # existing payload, bumps count and preserves first_seen_at, then writes
        # back with TTL. This is race-free under concurrent duplicates of the
        # same alert (the old GET-then-SETEX could lose increments).
        await redis_eval_int(
            _DEDUP_REMEMBER_LUA,
            1,
            webhook_dedupe(dedup_key),
            original_event_id,
            current_time,
            max(60, ttl_seconds),
            "1" if reset_chain else "0",
            dedup_key,
            analysis_json,
        )
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        REDIS_UNAVAILABLE_TOTAL.labels("dedup", "write_failed").inc()
        logger.warning(
            "[Dedup] Redis write failed; event still processed but the sliding window may be missing dedup_key=%s event_id=%s error_type=%s error=%s",
            dedup_key[:32] if dedup_key else "-",
            original_event_id,
            type(e).__name__,
            e,
            exc_info=True,
        )


# ── Dedup resolver ───────────────────────────────────────────────────────────


class DedupAction(StrEnum):
    NEW = "new"
    REUSE = "reuse"
    RECHAIN = "rechain"


@dataclass(frozen=True, slots=True)
class DedupResult:
    action: DedupAction
    analysis: dict[str, Any] | None
    original_event_id: int | None
    route_type: str = ""
    reset_chain: bool = False

    @property
    def is_duplicate(self) -> bool:
        return self.action == DedupAction.REUSE

    @property
    def is_rechain(self) -> bool:
        return self.action == DedupAction.RECHAIN


def _default_event_keys(
    data: Mapping[str, Any],
    source: str,
    *,
    namespace: str | None = None,
) -> tuple[str, str]:
    """Extract adapter identity once and generate both alert_hash and dedup_key."""
    from adapters.normalized import extract_alert_identity

    identity = extract_alert_identity(data)
    if identity:
        alert_key_fields: dict[str, object] = dict(identity)
        alert_key_fields.setdefault("source", source.strip().lower())
        if namespace:
            alert_key_fields["namespace"] = namespace
        alert_hash = hashlib.sha256(json.dumps_bytes(alert_key_fields, sort_keys=True)).hexdigest()

        dedup_key_fields: dict[str, object] = {}
        source_value = str(identity.get("source", source)).strip().lower()
        name_value = str(identity.get("name", "")).strip()
        if source_value:
            dedup_key_fields["source"] = source_value
        if name_value:
            dedup_key_fields["name"] = name_value
        resource = str(identity.get("resource", "") or "").strip()
        if resource:
            dedup_key_fields["resource"] = resource
        fingerprint = str(identity.get("fingerprint", "") or "").strip()
        if fingerprint:
            dedup_key_fields["fingerprint"] = fingerprint
        resource_bucket = resource_dedup_bucket(data)
        if resource_bucket:
            dedup_key_fields["resource_risk_bucket"] = resource_bucket
        if namespace:
            dedup_key_fields["namespace"] = namespace

        dedup_key = (
            hashlib.sha256(json.dumps_bytes(dedup_key_fields, sort_keys=True)).hexdigest()
            if dedup_key_fields
            else alert_hash
        )
        return alert_hash, dedup_key

    logger.debug("No alert identity produced by the adapter; falling back to a full-payload hash source=%s", source)
    fallback_key_fields: dict[str, object] = {"source": source.strip().lower(), "payload": data}
    if namespace:
        fallback_key_fields["namespace"] = namespace
    fallback_hash = hashlib.sha256(json.dumps_bytes(fallback_key_fields, sort_keys=True)).hexdigest()
    return fallback_hash, fallback_hash


# ── Per-source fingerprint fields (Keep's FINGERPRINT_FIELDS, on the mode ladder) ──


@lru_cache(maxsize=8)
def _parse_fingerprint_fields(raw: str) -> dict[str, tuple[str, ...]]:
    """Parse the JSON {source: [dot.paths]} setting; anything malformed is {}.

    Keyed on the raw string (mirrors url_security._parse_target_allowlist): the
    parse runs once per distinct setting value, not once per webhook, and a
    runtime-settings change produces a new key immediately.
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("[Dedup] DEDUP_FINGERPRINT_FIELDS is not valid JSON; fingerprint config ignored")
        return {}
    if not isinstance(loaded, dict):
        logger.warning("[Dedup] DEDUP_FINGERPRINT_FIELDS must be a JSON object; fingerprint config ignored")
        return {}
    parsed: dict[str, tuple[str, ...]] = {}
    for source, fields in loaded.items():
        if not isinstance(fields, list):
            continue
        cleaned = tuple(str(field).strip() for field in fields if str(field).strip())
        if cleaned:
            parsed[str(source).strip().lower()] = cleaned
    return parsed


def _fingerprint_mode() -> "FeatureMode":
    from services.operations.feature_modes import resolve_feature_mode

    cfg = get_config_manager().retry
    return resolve_feature_mode("DEDUP_FINGERPRINT_MODE", str(getattr(cfg, "DEDUP_FINGERPRINT_MODE", "off") or "off"))


def _fingerprint_fields_for_source(source: str) -> tuple[str, ...]:
    from services.operations import runtime_settings as rt

    cfg = get_config_manager().retry
    raw = rt.override_or("DEDUP_FINGERPRINT_FIELDS", str(getattr(cfg, "DEDUP_FINGERPRINT_FIELDS", "") or ""))
    return _parse_fingerprint_fields(raw).get(source.strip().lower(), ())


def _extract_field_path(data: Mapping[str, Any], path: str) -> object | None:
    """Resolve one dot-path against the payload; None when any hop is missing."""
    current: Any = data
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return None
            current = current[index] if -len(current) <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return cast(object, current)


def _fingerprint_dedup_key(
    data: Mapping[str, Any], source: str, fields: tuple[str, ...], namespace: str | None
) -> str | None:
    """Dedup key from the operator-configured identity fields; None if nothing matched."""
    values: dict[str, object] = {}
    for path in fields:
        value = _extract_field_path(data, path)
        if value is not None:
            values[path] = value
    if not values:
        return None
    key_fields: dict[str, object] = {"source": source.strip().lower(), "fields": values}
    if namespace:
        key_fields["namespace"] = namespace
    return hashlib.sha256(json.dumps_bytes(key_fields, sort_keys=True)).hexdigest()


# Where an alert's label map lives, most specific first. `alerts.0` rather than
# the whole list because the built-in grafana identity is also the FIRST alert's
# fingerprint (`_norm_grafana`), and an identity that keyed on a different alert
# than the adapter does would be a second, silent disagreement.
_LABEL_MAP_PATHS = ("alerts.0.labels", "commonLabels", "labels")


@lru_cache(maxsize=8)
def _parse_excluded_labels(raw: str) -> dict[str, frozenset[str]]:
    """Parse the JSON {source: [label names]} setting; anything malformed is {}."""
    text = raw.strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("[Dedup] DEDUP_FINGERPRINT_EXCLUDE_LABELS is not valid JSON; exclusion config ignored")
        return {}
    if not isinstance(loaded, dict):
        logger.warning("[Dedup] DEDUP_FINGERPRINT_EXCLUDE_LABELS must be a JSON object; exclusion config ignored")
        return {}
    parsed: dict[str, frozenset[str]] = {}
    for source, labels in loaded.items():
        if not isinstance(labels, list):
            continue
        cleaned = frozenset(str(label).strip() for label in labels if str(label).strip())
        if cleaned:
            parsed[str(source).strip().lower()] = cleaned
    return parsed


def _excluded_labels_for_source(source: str) -> frozenset[str]:
    from services.operations import runtime_settings as rt

    cfg = get_config_manager().retry
    raw = rt.override_or(
        "DEDUP_FINGERPRINT_EXCLUDE_LABELS",
        str(getattr(cfg, "DEDUP_FINGERPRINT_EXCLUDE_LABELS", "") or ""),
    )
    return _parse_excluded_labels(raw).get(source.strip().lower(), frozenset())


def _label_identity_dedup_key(
    data: Mapping[str, Any], source: str, excluded: frozenset[str], namespace: str | None
) -> str | None:
    """Dedup key from the alert's labels with the named volatile ones removed.

    The complement of DEDUP_FINGERPRINT_FIELDS, and the shape the Grafana case
    needs. Grafana computes its own `fingerprint` over the alert's LABELS, so a
    rule that embeds volatile detail in a label makes the upstream identity
    volatile too and every firing arrives as a new alert. Naming the identity
    fields instead (the inclusion list) fixes that rule and breaks the others:
    measured on production 2026-09-08, keying grafana on alertname+folder would
    have collapsed CertificateExpiredAlertRule's two threads (two different
    domains, `domain_name`/`certificate_arn`) into one and DatasourceNoData's
    four (four different `rulename`s) into one — the second expiring cert
    swallowed as a duplicate of the first.

    Dropping ONE label keeps every other label doing identity work, so those
    threads stay separate while the volatile one stops fragmenting its own.

    Returns None when no label map is found, or when the exclusions would empty
    it — falling back to the built-in key, because a config that matches nothing
    must fragment as before rather than collapse a whole source into one bucket.
    """
    for path in _LABEL_MAP_PATHS:
        labels = _extract_field_path(data, path)
        if not isinstance(labels, Mapping) or not labels:
            continue
        kept = {str(name): value for name, value in labels.items() if str(name) not in excluded}
        if not kept:
            return None
        key_fields: dict[str, object] = {"source": source.strip().lower(), "labels": kept}
        if namespace:
            key_fields["namespace"] = namespace
        return hashlib.sha256(json.dumps_bytes(key_fields, sort_keys=True)).hexdigest()
    return None


def generate_event_keys(
    data: Mapping[str, Any],
    source: str,
    *,
    namespace: str | None = None,
) -> tuple[str, str]:
    """(alert_hash, dedup_key) for one normalized payload.

    alert_hash always comes from the adapter identity — it names the alert for
    history and must not move under an operator's fingerprint experiments. The
    dedup_key (what threads repeats together) can be overridden per source two
    ways: DEDUP_FINGERPRINT_FIELDS names the fields that ARE the identity, and
    DEDUP_FINGERPRINT_EXCLUDE_LABELS names the labels that are NOT (identity
    becomes the remaining labels). Fields win when a source configures both.
    Either is rolled out on the off/shadow/enforce ladder:
    shadow computes the configured key and counts disagreement with the
    built-in one (signal dedup.fingerprint/diverged) while behaviour stays
    unchanged. A payload the configured paths do not match falls back to the
    built-in key in every mode — a half-matching config must fragment, not
    collapse everything into one bucket.
    """
    from core.observability.events import record_signal
    from services.operations.feature_modes import FeatureMode as _Mode

    alert_hash, dedup_key = _default_event_keys(data, source, namespace=namespace)
    mode = _fingerprint_mode()
    if mode is _Mode.OFF:
        return alert_hash, dedup_key
    fields = _fingerprint_fields_for_source(source)
    excluded = _excluded_labels_for_source(source)
    if not fields and not excluded:
        return alert_hash, dedup_key
    # Naming the identity fields outright wins over naming what is NOT identity:
    # the inclusion list is the more specific statement about the same source.
    configured = (
        _fingerprint_dedup_key(data, source, fields, namespace)
        if fields
        else _label_identity_dedup_key(data, source, excluded, namespace)
    )
    if configured is None:
        record_signal("dedup.fingerprint", "unextractable", {"webhook.source": source})
        return alert_hash, dedup_key
    if mode is _Mode.SHADOW:
        if configured != dedup_key:
            record_signal("dedup.fingerprint", "diverged", {"webhook.source": source})
        return alert_hash, dedup_key
    return alert_hash, configured


def generate_alert_hash(
    data: Mapping[str, Any],
    source: str,
    *,
    namespace: str | None = None,
) -> str:
    """Convenience wrapper — returns only the alert_hash portion of generate_event_keys."""
    return generate_event_keys(data, source, namespace=namespace)[0]


def _dedup_window_seconds() -> int:
    from services.operations import runtime_settings as rt

    return rt.override_or("DEDUP_WINDOW_SECONDS", int(get_config_manager().retry.DEDUP_WINDOW_SECONDS))


def _analysis_reuse_window_seconds() -> int:
    from services.operations import runtime_settings as rt

    return rt.override_or(
        "ANALYSIS_REUSE_WINDOW_SECONDS", int(get_config_manager().retry.ANALYSIS_REUSE_WINDOW_SECONDS)
    )


def _has_reusable_analysis(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return False
    return not is_analysis_degraded(analysis) and not is_pending_result(analysis)


async def _find_original_by_dedup_key(dedup_key: str, window_seconds: int) -> dict[str, Any] | None:
    from datetime import timedelta

    from sqlalchemy import select

    from models import WebhookEvent

    now = utcnow()
    threshold = now - timedelta(seconds=window_seconds)
    async with session_scope() as session:
        stmt = (
            select(WebhookEvent)
            .filter(
                WebhookEvent.dedup_key == dedup_key,
                WebhookEvent.timestamp >= threshold,
                WebhookEvent.is_duplicate.is_(False),
            )
            .order_by(WebhookEvent.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        original = result.scalar_one_or_none()
        if original and _has_reusable_analysis(original.ai_analysis):
            return {"analysis": dict(original.ai_analysis or {}), "original_event_id": original.id}
    return None


async def resolve_dedup(dedup_key: str) -> DedupResult:
    dedup_window = _dedup_window_seconds()
    analysis_reuse_window = _analysis_reuse_window_seconds()
    now = time.time()
    reset_chain_on_new = False

    state = await get_dedup_state(dedup_key)
    if state:
        reset_chain_on_new = (now - state.last_seen_at) > dedup_window
    if state and _has_reusable_analysis(state.analysis):
        first_seen_elapsed = now - state.first_seen_at
        last_seen_elapsed = now - state.last_seen_at

        # RECHAIN: the dedup window expired but the AI analysis is still within the
        # reuse window → create a new alert but reuse the analysis.
        if (
            first_seen_elapsed > dedup_window
            and last_seen_elapsed <= dedup_window
            and first_seen_elapsed <= analysis_reuse_window
        ):
            logger.info(
                "[Dedup] Alert chain exceeded the window, rebuilding chain dedup_key=%s orig_id=%s first_seen_elapsed=%ds dedup_window=%ds analysis_window=%ds count=%d",
                dedup_key[:32] if dedup_key else "-",
                state.original_event_id,
                int(first_seen_elapsed),
                dedup_window,
                analysis_reuse_window,
                state.count,
            )
            return DedupResult(
                action=DedupAction.RECHAIN,
                analysis=state.analysis,
                original_event_id=state.original_event_id,
                route_type="rechain",
                reset_chain=True,
            )

        # REUSE: hit within the normal dedup window
        if state.is_active(now, dedup_window):
            logger.debug(
                "[Dedup] Redis sliding-window hit dedup_key=%s orig_id=%s count=%d",
                dedup_key[:32] if dedup_key else "-",
                state.original_event_id,
                state.count,
            )
            return DedupResult(
                action=DedupAction.REUSE,
                analysis=state.analysis,
                original_event_id=state.original_event_id,
                route_type="redis_reuse",
            )

    db_result = await _find_original_by_dedup_key(dedup_key, dedup_window)
    if db_result:
        logger.debug(
            "[Dedup] DB fallback hit dedup_key=%s orig_id=%s",
            dedup_key[:32] if dedup_key else "-",
            db_result["original_event_id"],
        )
        return DedupResult(
            action=DedupAction.REUSE,
            analysis=db_result["analysis"],
            original_event_id=db_result["original_event_id"],
            route_type="db_reuse",
        )

    if reset_chain_on_new:
        logger.info(
            "[Dedup] Expired chain missed the DB fallback, creating a new chain dedup_key=%s previous_orig_id=%s",
            dedup_key[:32] if dedup_key else "-",
            state.original_event_id if state else "-",
        )

    return DedupResult(
        action=DedupAction.NEW,
        analysis=None,
        original_event_id=None,
        reset_chain=reset_chain_on_new,
    )


__all__ = [
    "DedupResult",
    "DedupState",
    "generate_alert_hash",
    "generate_event_keys",
    "get_dedup_state",
    "remember_dedup_state",
    "resolve_dedup",
]
