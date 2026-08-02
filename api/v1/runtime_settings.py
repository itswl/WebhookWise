"""Runtime settings API: live overrides for operator-policy config keys.

The read side lists every registered key with its env default, current
override, and effective value; writes validate against the setting registry
and broadcast a cross-process refresh. See
services/operations/runtime_settings.py for the plane's semantics.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, ok_response
from core.app_context import get_config_manager
from core.auth import verify_admin_write, verify_api_key
from core.datetime_utils import utc_isoformat
from core.logger import get_logger
from db.session import get_db_session
from models import RuntimeSetting
from services.operations.audit_logger import add_audit
from services.operations.runtime_settings import (
    SPECS,
    clear_override,
    publish_runtime_settings_invalidation,
    set_override,
)

logger = get_logger("api.v1.runtime_settings")

runtime_settings_router = APIRouter()


class RuntimeSettingWriteRequest(BaseModel):
    value: str = Field(min_length=0, max_length=500)
    actor: str = Field(default="", max_length=100)


def _env_value(key: str) -> Any:
    """The env/default value behind each registered key (display only)."""
    cfg = get_config_manager()
    getters: dict[str, Any] = {
        "FLAPPING_WINDOW_MINUTES": cfg.noise.FLAPPING_WINDOW_MINUTES,
        "FLAPPING_MIN_TRANSITIONS": cfg.noise.FLAPPING_MIN_TRANSITIONS,
        "FLAPPING_SUPPRESS_ENABLED": cfg.noise.FLAPPING_SUPPRESS_ENABLED,
        "INCIDENT_AUTO_SLA_MINUTES": cfg.notifications.INCIDENT_AUTO_SLA_MINUTES,
        "SLA_BREACH_MENTION_ALL": cfg.notifications.SLA_BREACH_MENTION_ALL,
        "WEBHOOK_MQ_BACKLOG_WARN_FRACTION": cfg.mq.WEBHOOK_MQ_BACKLOG_WARN_FRACTION,
        "WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION": cfg.mq.WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION,
        "WEBHOOK_INGRESS_STORM_THRESHOLD": cfg.mq.WEBHOOK_INGRESS_STORM_THRESHOLD
        or cfg.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD,
        "WEBHOOK_INGRESS_STORM_WINDOW_SECONDS": cfg.mq.WEBHOOK_INGRESS_STORM_WINDOW_SECONDS
        or cfg.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS,
        "KB_CARD_LINKS_ENABLED": cfg.kb.KB_CARD_LINKS_ENABLED,
        "KB_CARD_LINKS_MAX": cfg.kb.KB_CARD_LINKS_MAX,
        "ENABLE_ALERT_NOISE_REDUCTION": cfg.noise.ENABLE_ALERT_NOISE_REDUCTION,
        "NOISE_REDUCTION_WINDOW_MINUTES": cfg.noise.NOISE_REDUCTION_WINDOW_MINUTES,
        "ROOT_CAUSE_MIN_CONFIDENCE": cfg.noise.ROOT_CAUSE_MIN_CONFIDENCE,
        "NOISE_RELATED_MIN_CONFIDENCE": cfg.noise.NOISE_RELATED_MIN_CONFIDENCE,
        "NOISE_SOURCE_WEIGHT": cfg.noise.NOISE_SOURCE_WEIGHT,
        "NOISE_RESOURCE_WEIGHT": cfg.noise.NOISE_RESOURCE_WEIGHT,
        "NOISE_SEMANTIC_WEIGHT": cfg.noise.NOISE_SEMANTIC_WEIGHT,
        "NOISE_SEVERITY_WEIGHT": cfg.noise.NOISE_SEVERITY_WEIGHT,
        "NOISE_TIME_WEIGHT": cfg.noise.NOISE_TIME_WEIGHT,
        "NOISE_SEVERITY_DOWNGRADE_SCORE": cfg.noise.NOISE_SEVERITY_DOWNGRADE_SCORE,
        "SUPPRESS_DERIVED_ALERT_FORWARD": cfg.noise.SUPPRESS_DERIVED_ALERT_FORWARD,
        "NOTIFICATION_COOLDOWN_SECONDS": cfg.retry.NOTIFICATION_COOLDOWN_SECONDS,
        "ENABLE_PERIODIC_REMINDER": cfg.retry.ENABLE_PERIODIC_REMINDER,
        "REMINDER_INTERVAL_HOURS": cfg.retry.REMINDER_INTERVAL_HOURS,
        "DECISION_TRACE_RETENTION_DAYS": cfg.maintenance.DECISION_TRACE_RETENTION_DAYS,
    }
    return getters.get(key)


def _setting_dict(key: str, override_row: RuntimeSetting | None) -> dict[str, Any]:
    spec = SPECS[key]
    env_value = _env_value(key)
    if isinstance(env_value, bool):
        # Display symmetry with what the write path accepts/stores.
        env_value = "true" if env_value else "false"
    override = override_row.value if override_row is not None else None
    return {
        "key": key,
        "domain": spec.domain,
        "description": spec.description,
        "env_value": str(env_value) if env_value is not None else "",
        "override": override,
        "effective": override if override is not None else (str(env_value) if env_value is not None else ""),
        "updated_by": override_row.updated_by if override_row is not None else "",
        "updated_at": utc_isoformat(override_row.updated_at) if override_row is not None else None,
    }


@runtime_settings_router.get("/runtime-settings", dependencies=[Depends(verify_api_key)])
async def list_runtime_settings_endpoint(session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """All registered runtime-policy keys with env default / override / effective."""
    rows = {row.key: row for row in (await session.execute(select(RuntimeSetting))).scalars().all()}
    settings = [_setting_dict(key, rows.get(key)) for key in SPECS]
    return ok_response(http_status=200, data={"settings": settings})


@runtime_settings_router.put("/runtime-settings/{key}", dependencies=[Depends(verify_admin_write)])
async def put_runtime_setting_endpoint(
    key: str,
    request: RuntimeSettingWriteRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Set one override (validated against the registry); live within ~1 min."""
    if key not in SPECS:
        return fail_response(f"unknown runtime setting {key!r}", 404)
    try:
        row = await set_override(session, key, request.value, actor=request.actor or "dashboard")
    except ValueError as e:
        return fail_response(str(e), 400)
    add_audit(
        session,
        "runtime_setting",
        0,
        key,
        "updated",
        f"Runtime override set: {key}={request.value}",
        actor=request.actor or "dashboard",
    )
    await session.commit()
    await publish_runtime_settings_invalidation()
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:runtime_setting_changed")
    logger.info("[RuntimeSettings] Override set key=%s by=%s", key, request.actor or "dashboard")
    return ok_response(http_status=200, message="override set", data=_setting_dict(key, row))


@runtime_settings_router.delete("/runtime-settings/{key}", dependencies=[Depends(verify_admin_write)])
async def delete_runtime_setting_endpoint(
    key: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Clear one override — the key falls back to its env value / default."""
    if key not in SPECS:
        return fail_response(f"unknown runtime setting {key!r}", 404)
    removed = await clear_override(session, key)
    if not removed:
        return ok_response(http_status=200, message="no override was set", data=_setting_dict(key, None))
    add_audit(session, "runtime_setting", 0, key, "cleared", f"Runtime override cleared: {key}")
    await session.commit()
    await publish_runtime_settings_invalidation()
    logger.info("[RuntimeSettings] Override cleared key=%s", key)
    return ok_response(http_status=200, message="override cleared", data=_setting_dict(key, None))
