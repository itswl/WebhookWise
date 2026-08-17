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


# Keys whose env default is a computed fallback chain rather than one field.
_ENV_VALUE_SPECIAL: dict[str, Any] = {
    "WEBHOOK_INGRESS_STORM_THRESHOLD": lambda cfg: cfg.mq.WEBHOOK_INGRESS_STORM_THRESHOLD
    or cfg.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD,
    "WEBHOOK_INGRESS_STORM_WINDOW_SECONDS": lambda cfg: cfg.mq.WEBHOOK_INGRESS_STORM_WINDOW_SECONDS
    or cfg.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS,
}

_CONFIG_GROUPS = (
    "noise",
    "ai",
    "kb",
    "notifications",
    "deep_analysis",
    "circuit_breaker",
    "retry",
    "maintenance",
    "mq",
    "security",
    "tasks",
    "server",
)


def _env_value(key: str) -> Any:
    """The env/default value behind each registered key (display only).

    Resolved by name across the config groups instead of a hand-maintained
    map: the map silently fell behind the registry (53 of 80 keys showed a
    blank env-default column on the dashboard, including a key added the day
    it shipped). Registered keys are unique across groups — the registry is
    the namespace.
    """
    cfg = get_config_manager()
    special = _ENV_VALUE_SPECIAL.get(key)
    if special is not None:
        return special(cfg)
    for group_name in _CONFIG_GROUPS:
        group = getattr(cfg, group_name, None)
        if group is not None and hasattr(group, key):
            return getattr(group, key)
    return None


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
