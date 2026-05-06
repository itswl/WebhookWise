import os

from core.config import Config
from core.logger import logger

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
        # 如果前端显式传了空字符串，我们应该允许覆盖为空（或删除配置），但如果是 API Key 等，我们要额外处理
        # 为兼容已有逻辑，返回特殊标记以删除或覆盖。此处直接返回空字符串
        return "", ""
    if validator and not validator(typed_value):
        raise ValueError(f"{key} 格式无效")
    return typed_value, typed_value


def collect_config_updates(payload: dict) -> tuple[dict, list[str]]:
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


def get_current_config():
    response = {}
    for field_name, (env_var, _value_type, _validator) in _CONFIG_SCHEMA.items():
        runtime_info = Config.RUNTIME_KEYS.get(env_var)
        sub_name = runtime_info["sub"] if runtime_info else None
        value = getattr(getattr(Config, sub_name), env_var, "") if sub_name else ""

        if env_var == "OPENAI_API_KEY" and value:
            response[field_name] = "已配置"
        else:
            response[field_name] = value
    return response


def get_config_sources():
    keys = sorted(set(Config.RUNTIME_KEYS.keys()))
    items = []
    for key in keys:
        meta = Config.get_meta(key)
        source = meta.get("source")
        if not source:
            source = "env" if os.getenv(key) is not None else "default"
        updated_at = meta.get("updated_at")
        if updated_at is not None:
            updated_at = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        items.append(
            {
                "key": key,
                "source": str(source),
                "updated_at": updated_at,
                "updated_by": meta.get("updated_by"),
            }
        )
    return items


def build_prompt_source():
    if Config.ai.AI_USER_PROMPT:
        return "environment"
    if Config.ai.AI_USER_PROMPT_FILE:
        return "file"
    return "default"
