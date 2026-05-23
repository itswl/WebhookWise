import os
from collections.abc import Mapping
from typing import Any

from core.app_context import get_config_manager
from core.config import UnifiedConfigManager

_CONFIG_FIELDS: Mapping[str, str] = {
    "default_target_url": "DEFAULT_FORWARD_TARGET_URL",
    "enable_forward": "ENABLE_FORWARD",
    "enable_ai_analysis": "ENABLE_AI_ANALYSIS",
    "openai_api_key": "OPENAI_API_KEY",
    "openai_api_url": "OPENAI_API_URL",
    "openai_model": "OPENAI_MODEL",
    "ai_system_prompt": "AI_SYSTEM_PROMPT",
    "ai_user_prompt": "AI_USER_PROMPT",
    "ai_user_prompt_file": "AI_USER_PROMPT_FILE",
    "deep_analysis_prompt": "DEEP_ANALYSIS_PROMPT",
    "deep_analysis_prompt_file": "DEEP_ANALYSIS_PROMPT_FILE",
    "log_level": "LOG_LEVEL",
    "third_party_log_level": "THIRD_PARTY_LOG_LEVEL",
    "duplicate_alert_time_window": "DUPLICATE_ALERT_TIME_WINDOW",
    "forward_duplicate_alerts": "FORWARD_DUPLICATE_ALERTS",
    "reanalyze_after_time_window": "REANALYZE_AFTER_TIME_WINDOW",
    "forward_after_time_window": "FORWARD_AFTER_TIME_WINDOW",
    "enable_alert_noise_reduction": "ENABLE_ALERT_NOISE_REDUCTION",
    "noise_reduction_window_minutes": "NOISE_REDUCTION_WINDOW_MINUTES",
    "root_cause_min_confidence": "ROOT_CAUSE_MIN_CONFIDENCE",
    "noise_related_min_confidence": "NOISE_RELATED_MIN_CONFIDENCE",
    "noise_source_weight": "NOISE_SOURCE_WEIGHT",
    "noise_resource_weight": "NOISE_RESOURCE_WEIGHT",
    "noise_semantic_weight": "NOISE_SEMANTIC_WEIGHT",
    "noise_severity_weight": "NOISE_SEVERITY_WEIGHT",
    "noise_time_weight": "NOISE_TIME_WEIGHT",
    "noise_severity_downgrade_score": "NOISE_SEVERITY_DOWNGRADE_SCORE",
    "suppress_derived_alert_forward": "SUPPRESS_DERIVED_ALERT_FORWARD",
}


def _get_config_value(env_var: str, config: UnifiedConfigManager) -> Any:
    config_info = config.CONFIG_KEYS.get(env_var)
    if not config_info:
        return ""
    return getattr(getattr(config, config_info["sub"]), env_var, "")


def _get_config_source(env_var: str) -> str:
    return "file_or_environment" if os.getenv(env_var) is not None else "default"


def get_current_config() -> dict[str, object]:
    config = get_config_manager()
    response: dict[str, object] = {}
    for field_name, env_var in _CONFIG_FIELDS.items():
        value = _get_config_value(env_var, config)
        response[field_name] = "已配置" if env_var == "OPENAI_API_KEY" and value else value
    return response


def get_config_sources() -> list[dict[str, object]]:
    config = get_config_manager()
    return [
        {
            "key": key,
            "source": _get_config_source(key),
            "requires_restart": True,
        }
        for key in sorted(config.CONFIG_KEYS.keys())
    ]
