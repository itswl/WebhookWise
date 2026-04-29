from fastapi import APIRouter

from api import _fail, _ok
from core.config import Config
from core.logger import logger
from core.runtime_config import _KEY_TO_SUBCONFIG, runtime_config

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


@admin_router.get("/api/config")
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


@admin_router.post("/api/config")
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


@admin_router.post("/api/prompt/reload")
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


@admin_router.get("/api/prompt")
def get_prompt():
    try:
        template = _load_current_prompt_template()
        return _ok(status=200, template=template, source=_build_prompt_source())
    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post("/api/migrations/add_unique_constraint")
def migration_add_unique_constraint():
    try:
        success = _run_add_unique_constraint_migration()
        if success:
            return _ok(status=200, message="数据库迁移成功：唯一约束已添加")
        return _fail("数据库迁移失败，请查看日志", 500)
    except Exception as e:
        logger.error(f"执行迁移失败: {e}")
        return _fail(str(e), 500)
