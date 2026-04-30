from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api import _fail, _ok
from core.auth import verify_admin_write
from core.config import Config
from core.logger import logger
from core.redis_client import get_redis
from core.runtime_config import _KEY_TO_SUBCONFIG, runtime_config
from crud.webhook import count_dead_letters, list_dead_letters, replay_dead_letter
from db.session import get_db_session
from schemas.admin import (
    ConfigResponse,
    ConfigUpdateResponse,
    DeadLetterListResponse,
    PromptGetResponse,
    PromptReloadResponse,
    ReplayAllResponse,
    ReplayResponse,
)

admin_router = APIRouter()


_CONFIG_SCHEMA = {
    "forward_url": ("FORWARD_URL", "str", lambda x: x.startswith("http")),
    "enable_forward": ("ENABLE_FORWARD", "bool", None),
    "enable_ai_analysis": ("ENABLE_AI_ANALYSIS", "bool", None),
    "openai_api_key": ("OPENAI_API_KEY", "str", None),
    "openai_api_url": ("OPENAI_API_URL", "str", lambda x: x.startswith("http")),
    "openai_model": ("OPENAI_MODEL", "str", lambda x: len(x) > 0),
    "ai_system_prompt": ("AI_SYSTEM_PROMPT", "str", None),
    "log_level": ("LOG_LEVEL", "str", lambda x: x.upper() in ["DEBUG", "INFO", "WARNING", "ERROR"]),
    "duplicate_alert_time_window": ("DUPLICATE_ALERT_TIME_WINDOW", "int", lambda x: 1 <= x <= 168),
    "forward_duplicate_alerts": ("FORWARD_DUPLICATE_ALERTS", "bool", None),
    "reanalyze_after_time_window": ("REANALYZE_AFTER_TIME_WINDOW", "bool", None),
    "forward_after_time_window": ("FORWARD_AFTER_TIME_WINDOW", "bool", None),
    "enable_alert_noise_reduction": ("ENABLE_ALERT_NOISE_REDUCTION", "bool", None),
    "noise_reduction_window_minutes": ("NOISE_REDUCTION_WINDOW_MINUTES", "int", lambda x: 1 <= x <= 60),
    "root_cause_min_confidence": ("ROOT_CAUSE_MIN_CONFIDENCE", "float", lambda x: 0 <= x <= 1),
    "suppress_derived_alert_forward": ("SUPPRESS_DERIVED_ALERT_FORWARD", "bool", None),
}


def _parse_update_value(key: str, raw_value, value_type: str, validator):
    if value_type == "bool":
        if isinstance(raw_value, bool):
            typed_value = raw_value
        elif isinstance(raw_value, str):
            typed_value = raw_value.lower() == "true"
        else:
            raise ValueError(f"{key} 应为布尔类型")
        return str(typed_value).lower(), typed_value

    if value_type == "int":
        typed_value = int(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    if value_type == "float":
        typed_value = float(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    typed_value = str(raw_value).strip()
    if not typed_value:
        return None, None
    if validator and not validator(typed_value):
        raise ValueError(f"{key} 格式无效")
    return typed_value, typed_value


def _collect_config_updates(payload: dict) -> tuple[dict, list[str]]:
    updates = {}
    errors = []

    for key, raw_value in payload.items():
        if key not in _CONFIG_SCHEMA:
            continue

        env_var, value_type, validator = _CONFIG_SCHEMA[key]
        try:
            string_value, typed_value = _parse_update_value(key, raw_value, value_type, validator)
            if string_value is None:
                logger.debug(f"跳过空值配置: {key}")
                continue
            updates[env_var] = (string_value, typed_value)
        except ValueError as e:
            errors.append(str(e))

    return updates, errors


def _build_prompt_source() -> str:
    if Config.ai.AI_USER_PROMPT:
        return "environment"
    if Config.ai.AI_USER_PROMPT_FILE:
        return "file"
    return "default"


def _reload_prompt_template() -> str:
    from services.ai_prompts import reload_user_prompt_template

    new_template = reload_user_prompt_template()
    logger.info("AI Prompt 模板已重新加载")
    return new_template


def _load_current_prompt_template() -> str:
    from services.ai_prompts import load_user_prompt_template

    return load_user_prompt_template()


def _run_add_unique_constraint_migration() -> bool:
    from migrations.migrations_tool import add_unique_constraint

    logger.info("开始执行数据库迁移：添加唯一约束")
    return add_unique_constraint()


@admin_router.get("/api/config", response_model=ConfigResponse)
async def get_config():
    try:
        response = {}
        for field_name, (env_var, _value_type, _validator) in _CONFIG_SCHEMA.items():
            sub_name = _KEY_TO_SUBCONFIG.get(env_var)
            value = getattr(getattr(Config, sub_name), env_var, "") if sub_name else ""
            if env_var == "OPENAI_API_KEY" and value:
                response[field_name] = "已配置"
            else:
                response[field_name] = value
        return _ok(response, 200)
    except Exception as e:
        logger.error(f"获取配置失败: {e!s}")
        return _fail(str(e), 500)


@admin_router.post("/api/config", response_model=ConfigUpdateResponse, dependencies=[Depends(verify_admin_write)])
async def update_config(payload: dict | None = None):
    try:
        if not payload:
            return _fail("请求体为空", 400)

        updates, errors = _collect_config_updates(payload)
        if errors:
            return _fail("; ".join(errors), 400)

        if not updates:
            return _ok(status=200, message="无需更新")

        runtime_updates = {var_name: typed_value for var_name, (_str_val, typed_value) in updates.items()}
        await runtime_config.save_batch(runtime_updates)

        logger.info(f"配置已更新: {list(updates.keys())}")
        return _ok(status=200, message=f"配置更新成功，已保存 {len(runtime_updates)} 项")

    except Exception as e:
        logger.error(f"更新配置失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/prompt/reload", response_model=PromptReloadResponse)
def reload_prompt():
    try:
        new_template = _reload_prompt_template()
        return _ok(
            status=200,
            message="Prompt 模板已重新加载",
            template_length=len(new_template),
            preview=new_template[:200] + "..." if len(new_template) > 200 else new_template,
        )
    except Exception as e:
        logger.error(f"重新加载 prompt 模板失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.get("/api/prompt", response_model=PromptGetResponse)
def get_prompt():
    try:
        template = _load_current_prompt_template()
        return _ok(status=200, template=template, source=_build_prompt_source())
    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/migrations/add_unique_constraint", dependencies=[Depends(verify_admin_write)])
def migration_add_unique_constraint():
    try:
        success = _run_add_unique_constraint_migration()
        if success:
            return _ok(status=200, message="数据库迁移成功：唯一约束已添加")
        return _fail("数据库迁移失败，请查看日志", 500)
    except Exception as e:
        logger.error(f"执行迁移失败: {e}")
        return _fail(str(e), 500)


# ── Dead Letter 重放 ──────────────────────────────────────────────────────────


@admin_router.get("/api/admin/dead-letters", response_model=DeadLetterListResponse)
async def get_dead_letters(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """列出所有 dead_letter 事件（分页）"""
    try:
        items = await list_dead_letters(session, page=page, page_size=page_size)
        total = await count_dead_letters(session)
        # 序列化 datetime 字段
        for item in items:
            if item.get("timestamp"):
                item["timestamp"] = item["timestamp"].isoformat()
        return _ok(
            data=items,
            http_status=200,
            pagination={"page": page, "page_size": page_size, "total": total},
        )
    except Exception as e:
        logger.error(f"查询 dead_letter 列表失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/{event_id}/replay",
    response_model=ReplayResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def replay_single_dead_letter(
    event_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """重放单个 dead_letter：重置状态 + 重新投递 Redis Stream"""
    try:
        updated = await replay_dead_letter(session, event_id)
        if not updated:
            return _fail(f"事件 {event_id} 不存在或状态非 dead_letter", 404)

        # 先提交 DB，再投递 Redis Stream（与 api/webhook.py 保持一致）
        await session.commit()

        redis = get_redis()
        await redis.xadd(
            Config.server.WEBHOOK_MQ_QUEUE,
            {"event_id": str(event_id), "client_ip": ""},
            maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
            approximate=True,
        )

        logger.info(f"[Admin] Dead letter 重放成功: event_id={event_id}")
        return _ok(http_status=200, message=f"事件 {event_id} 已重放", event_id=event_id)
    except Exception as e:
        logger.error(f"重放 dead_letter 失败: event_id={event_id}, error={e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post(
    "/api/admin/dead-letters/replay-all", response_model=ReplayAllResponse, dependencies=[Depends(verify_admin_write)]
)
async def replay_all_dead_letters(
    batch_size: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
):
    """批量重放所有 dead_letter（限流，最多 batch_size 条）"""
    try:
        items = await list_dead_letters(session, page=1, page_size=batch_size)
        if not items:
            return _ok(http_status=200, message="无 dead_letter 需要重放", replayed=0)

        replayed_ids = []
        for item in items:
            eid = item["id"]
            updated = await replay_dead_letter(session, eid)
            if updated:
                replayed_ids.append(eid)

        # 先提交 DB，再批量投递 Redis Stream
        await session.commit()

        redis = get_redis()
        for eid in replayed_ids:
            await redis.xadd(
                Config.server.WEBHOOK_MQ_QUEUE,
                {"event_id": str(eid), "client_ip": ""},
                maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
                approximate=True,
            )

        logger.info(f"[Admin] 批量重放完成: {len(replayed_ids)} 条")
        return _ok(
            http_status=200,
            message=f"已重放 {len(replayed_ids)} 条 dead_letter",
            replayed=len(replayed_ids),
            event_ids=replayed_ids,
        )
    except Exception as e:
        logger.error(f"批量重放 dead_letter 失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)
