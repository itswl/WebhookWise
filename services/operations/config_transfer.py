"""Operational config as a portable bundle: export / import (upsert).

Covers the three operator-authored collections — forward rules, silences, and
maintenance windows — so a deployment's routing/muting setup can be backed up,
reviewed in Git, or replayed onto another environment. Import is additive:
rows are created or updated by natural key, never deleted.

Natural keys:
- forward rule → ``name`` (duplicate names in the DB make that name
  un-importable; reported as an error rather than guessing)
- maintenance window → ``name`` (unique-constrained)
- silence → the full match tuple + comment; only ACTIVE silences are exported,
  and maintenance-materialized silences (created_by="maintenance-window") are
  excluded — they are derived state owned by their window.

The bundle contains forwarding target URLs (bot tokens), so both directions
are write-key-gated at the API layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger
from models import ForwardRule, MaintenanceWindow, Silence

logger = get_logger("operations.config_transfer")


def _silence_active_filter() -> ColumnElement[bool]:
    now = utcnow()
    return Silence.lifted_at.is_(None) & or_(Silence.expires_at.is_(None), Silence.expires_at > now)


BUNDLE_VERSION = 1

_RULE_FIELDS = (
    "name",
    "enabled",
    "priority",
    "match_event_type",
    "match_importance",
    "match_duplicate",
    "match_source",
    "match_project",
    "match_region",
    "match_environment",
    "match_payload",
    "target_type",
    "target_url",
    "target_name",
    "stop_on_match",
)
_SILENCE_MATCH_FIELDS = (
    "match_source",
    "match_importance",
    "match_event_type",
    "match_project",
    "match_region",
    "match_environment",
    "match_payload",
)
_SILENCE_FIELDS = (*_SILENCE_MATCH_FIELDS, "comment", "created_by")
_WINDOW_FIELDS = (
    "name",
    "enabled",
    "match_source",
    "match_importance",
    "match_event_type",
    "match_project",
    "match_region",
    "match_environment",
    "match_payload",
    "days_of_week",
    "start_minute",
    "duration_minutes",
    "timezone",
    "comment",
    "created_by",
)

_MAINTENANCE_CREATED_BY = "maintenance-window"


def _row_dict(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(row, field) for field in fields}


async def export_config(session: AsyncSession) -> dict[str, Any]:
    rules = (await session.execute(select(ForwardRule).order_by(ForwardRule.name, ForwardRule.id))).scalars().all()
    silences = (
        (
            await session.execute(
                select(Silence).where(_silence_active_filter(), Silence.created_by != _MAINTENANCE_CREATED_BY)
            )
        )
        .scalars()
        .all()
    )
    windows = (await session.execute(select(MaintenanceWindow).order_by(MaintenanceWindow.name))).scalars().all()
    return {
        "version": BUNDLE_VERSION,
        "exported_at": utc_isoformat(utcnow()),
        "forward_rules": [_row_dict(r, _RULE_FIELDS) for r in rules],
        "silences": [{**_row_dict(s, _SILENCE_FIELDS), "expires_at": utc_isoformat(s.expires_at)} for s in silences],
        "maintenance_windows": [_row_dict(w, _WINDOW_FIELDS) for w in windows],
    }


def _clean(entry: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("entry must be a mapping")
    return {field: entry[field] for field in fields if field in entry}


def _apply(row: Any, data: dict[str, Any]) -> bool:
    changed = False
    for field, value in data.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


async def import_config(session: AsyncSession, bundle: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Upsert a bundle; returns a per-collection created/updated/unchanged report.

    On dry_run the session is still mutated in memory for accurate reporting —
    the CALLER must roll back instead of committing.
    """
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a mapping")
    if int(bundle.get("version") or 0) != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version {bundle.get('version')!r} (expected {BUNDLE_VERSION})")

    report: dict[str, Any] = {"dry_run": dry_run}

    # Forward rules by name.
    rule_report: dict[str, Any] = {"created": 0, "updated": 0, "unchanged": 0, "errors": []}
    existing_rules: dict[str, list[ForwardRule]] = {}
    for rule in (await session.execute(select(ForwardRule))).scalars().all():
        existing_rules.setdefault(str(rule.name), []).append(rule)
    for entry in bundle.get("forward_rules") or []:
        try:
            data = _clean(entry, _RULE_FIELDS)
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("forward rule without a name")
            matches = existing_rules.get(name, [])
            if len(matches) > 1:
                raise ValueError(f"forward rule name {name!r} is ambiguous in the target (multiple rows)")
            if matches:
                rule_report["updated" if _apply(matches[0], data) else "unchanged"] += 1
            else:
                session.add(ForwardRule(**data))
                rule_report["created"] += 1
        except (KeyError, TypeError, ValueError) as e:
            rule_report["errors"].append(str(e))
    report["forward_rules"] = rule_report

    # Maintenance windows by (unique) name.
    window_report: dict[str, Any] = {"created": 0, "updated": 0, "unchanged": 0, "errors": []}
    existing_windows = {str(w.name): w for w in (await session.execute(select(MaintenanceWindow))).scalars().all()}
    for entry in bundle.get("maintenance_windows") or []:
        try:
            data = _clean(entry, _WINDOW_FIELDS)
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("maintenance window without a name")
            window = existing_windows.get(name)
            if window is not None:
                window_report["updated" if _apply(window, data) else "unchanged"] += 1
            else:
                session.add(MaintenanceWindow(**data))
                window_report["created"] += 1
        except (KeyError, TypeError, ValueError) as e:
            window_report["errors"].append(str(e))
    report["maintenance_windows"] = window_report

    # Silences by match tuple + comment, among currently-active rows.
    silence_report: dict[str, Any] = {"created": 0, "updated": 0, "unchanged": 0, "errors": []}
    active_silences = (
        (
            await session.execute(
                select(Silence).where(_silence_active_filter(), Silence.created_by != _MAINTENANCE_CREATED_BY)
            )
        )
        .scalars()
        .all()
    )

    def _silence_key(data: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(data.get(f) or "") for f in (*_SILENCE_MATCH_FIELDS, "comment"))

    existing_by_key = {_silence_key(_row_dict(s, _SILENCE_FIELDS)): s for s in active_silences}
    for entry in bundle.get("silences") or []:
        try:
            data = _clean(entry, _SILENCE_FIELDS)
            expires_raw = entry.get("expires_at") if isinstance(entry, dict) else None
            expires_at = None
            if expires_raw:
                parsed_ts = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                if parsed_ts.tzinfo is not None:
                    parsed_ts = parsed_ts.astimezone(UTC).replace(tzinfo=None)
                expires_at = parsed_ts
            key = _silence_key(data)
            if not any(v for v in key[:-1]):
                raise ValueError("silence without any match criterion")
            silence = existing_by_key.get(key)
            if silence is not None:
                changed = _apply(silence, {"expires_at": expires_at}) if "expires_at" in (entry or {}) else False
                silence_report["updated" if changed else "unchanged"] += 1
            else:
                session.add(Silence(**data, expires_at=expires_at))
                silence_report["created"] += 1
        except (KeyError, TypeError, ValueError) as e:
            silence_report["errors"].append(str(e))
    report["silences"] = silence_report

    await session.flush()
    return report
