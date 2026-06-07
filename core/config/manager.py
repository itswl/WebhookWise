"""Static configuration metadata backed by ``core.config.defaults.get_settings``."""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Literal, TypedDict, get_args, get_origin

from pydantic import ValidationError
from pydantic_settings import BaseSettings

from core.config.defaults import AppConfig, get_settings

ConfigValueType = Literal["str", "int", "float", "bool"]


class ConfigKeyMeta(TypedDict):
    type: ConfigValueType
    sub: str


_CONFIG_TYPE_BY_PY_TYPE: dict[type[object], ConfigValueType] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
}


def _config_type_for_annotation(annotation: object) -> ConfigValueType | None:
    if annotation in _CONFIG_TYPE_BY_PY_TYPE:
        return _CONFIG_TYPE_BY_PY_TYPE[annotation]
    if get_origin(annotation) is not None:
        for arg in get_args(annotation):
            if arg in _CONFIG_TYPE_BY_PY_TYPE:
                return _CONFIG_TYPE_BY_PY_TYPE[arg]
    return None


@lru_cache
def get_config_keys() -> dict[str, ConfigKeyMeta]:
    settings = get_settings()
    keys: dict[str, ConfigKeyMeta] = {}
    for sub_name in settings._SUB_NAMES:
        sub_config: BaseSettings = getattr(settings, sub_name)
        for key, field in type(sub_config).model_fields.items():
            value_type = _config_type_for_annotation(field.annotation)
            if value_type is None:
                continue
            keys[key] = {"type": value_type, "sub": sub_name}
    return keys


# ── 配置热加载 ────────────────────────────────────────────────────────────────

_config_version: int = 0


def get_config_version() -> int:
    """Return the current config version (monotonic counter incremented on reload)."""
    return _config_version


_RELOADABLE_SECTIONS: set[str] = {
    "noise",
    "retry",
    "ai",
    "circuit_breaker",
    "notifications",
    "maintenance",
    "tasks",
    "security",
    "openclaw",
}

_SECTION_NAMES_CN: dict[str, str] = {
    "noise": "降噪配置",
    "retry": "重试配置",
    "ai": "AI 配置",
    "circuit_breaker": "熔断器配置",
    "notifications": "通知配置",
    "maintenance": "维护配置",
    "tasks": "任务配置",
    "security": "安全配置",
    "openclaw": "OpenClaw 配置",
    "server": "服务配置",
    "db": "数据库配置",
    "redis": "Redis 配置",
    "mq": "消息队列配置",
    "all": "全部配置",
}

_NOT_HOT_RELOADABLE: set[str] = {"server", "db", "redis", "mq"}


def get_reloadable_sections() -> list[dict[str, str]]:
    """Return metadata for all config sections (reloadable + non-reloadable)."""
    settings = get_settings()
    sections: list[dict[str, str]] = []
    for sub_name in settings._SUB_NAMES:
        hot = "yes" if sub_name in _RELOADABLE_SECTIONS else "no"
        cn = _SECTION_NAMES_CN.get(sub_name, sub_name)
        sections.append({"id": sub_name, "name": cn, "hot_reloadable": hot})
    return sections


def _get_logger():
    """Lazy import to avoid circular dependency (logger -> config -> logger)."""
    from core.logger import get_logger

    return get_logger("config")


def _reload_env_overrides() -> None:
    """Re-read .env file into environment variables to pick up changes."""
    env_file = os.environ.get("WEBHOOKWISE_ENV_FILE", ".env")
    if not os.path.isfile(env_file):
        return
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def reload_config(section: str) -> dict[str, object]:
    """Reload one or all configuration sections from environment variables.

    This clears the internal LRU cache on ``get_settings()`` and re-reads
    the ``.env`` file. For a targeted reload it reconstructs only the
    requested sub-config and re-stitches it into a fresh ``AppConfig``.

    Returns a summary dict with keys ``reloaded_sections``, ``settings``
    (number of settings reloaded), and ``errors``.

    Use ``section="all"`` to reload everything.
    """
    global _config_version
    logger = _get_logger()
    section = section.strip().lower()
    supported: set[str] = _RELOADABLE_SECTIONS | {"all"}

    if section not in supported:
        # 检查是否是不可热更新的 section
        if section in _NOT_HOT_RELOADABLE:
            cn_name = _SECTION_NAMES_CN.get(section, section)
            msg = (
                f"配置段 '{section}' ({cn_name}) 不支持热更新，需要重启进程生效。"
                f" 可热更新的节: {', '.join(sorted(supported))}"
            )
            logger.warning("[ConfigReload] %s", msg)
            return {
                "success": False,
                "error": msg,
                "not_hot_reloadable": True,
            }
        msg = f"未知配置段 '{section}'。支持的节: {', '.join(sorted(supported))}"
        logger.warning("[ConfigReload] %s", msg)
        return {"success": False, "error": msg, "not_hot_reloadable": False}

    # Re-read .env so new values appear in the environment
    _reload_env_overrides()

    # Clear the cached default_settings.get_settings()
    get_settings.cache_clear()
    try:
        fresh = get_settings()
    except ValidationError as exc:
        logger.error("[ConfigReload] Validation error after reload: %s", exc)
        return {"success": False, "error": f"Validation error: {exc}"}

    # Build a human summary
    field_names: list[str] = []
    if section == "all":
        field_names = list(AppConfig.model_fields)
    elif section == "circuit_breaker":
        field_names = ["circuit_breaker", "retry"]
    else:
        field_names = [section]

    count = 0
    for name in field_names:
        sub = getattr(fresh, name, None)
        if sub is not None:
            count += len(sub.model_fields)

    _config_version += 1

    logger.info(
        "[ConfigReload] 配置已重新加载 section=%s sub_sections=%s settings=%d version=%d",
        section,
        field_names,
        count,
        _config_version,
    )
    return {
        "success": True,
        "section": section,
        "sub_sections": field_names,
        "settings_count": count,
        "version": _config_version,
    }


# ── 文件监听 ──────────────────────────────────────────────────────────────────

_env_mtime: float = 0.0
_env_file_path: str = ""


def _init_env_watch() -> None:
    """Initialize the env file path and record its current mtime."""
    global _env_file_path, _env_mtime
    _env_file_path = os.environ.get("WEBHOOKWISE_ENV_FILE", ".env")
    _env_mtime = os.path.getmtime(_env_file_path) if os.path.isfile(_env_file_path) else 0.0


async def watch_env_file(interval: float = 5.0) -> None:
    """Background asyncio task: poll .env mtime every ``interval`` seconds.

    If the file changes, automatically trigger a full config reload.
    """
    global _env_mtime
    logger = _get_logger()
    _init_env_watch()
    while True:
        await asyncio.sleep(interval)
        try:
            if not os.path.isfile(_env_file_path):
                continue
            current_mtime = os.path.getmtime(_env_file_path)
            if current_mtime != _env_mtime:
                _env_mtime = current_mtime
                logger.info(
                    "[ConfigWatch] .env 文件已变更 (mtime: %s)，自动触发配置重载", current_mtime
                )
                reload_config("all")
        except OSError:
            continue
