"""Static configuration facade backed by ``core.config.defaults.get_settings``."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Generic, Literal, TypedDict, TypeVar, cast, get_args, get_origin, overload

from pydantic_settings import BaseSettings

from core.config.defaults import (
    AIConfig,
    AppConfig,
    CircuitBreakerConfig,
    DBConfig,
    MaintenanceConfig,
    MQConfig,
    NoiseConfig,
    NotificationConfig,
    OpenClawConfig,
    RedisConfig,
    RetryConfig,
    SecurityConfig,
    ServerConfig,
    TaskConfig,
    get_settings,
)

ConfigValueType = Literal["str", "int", "float", "bool"]
_ConfigSectionT = TypeVar("_ConfigSectionT", bound=BaseSettings)


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


class _ConfigSection(Generic[_ConfigSectionT]):
    def __init__(self, name: str) -> None:
        self._name = name

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> _ConfigSection[_ConfigSectionT]: ...

    @overload
    def __get__(self, instance: object, owner: type[Any]) -> _ConfigSectionT: ...

    def __get__(
        self, instance: object | None, owner: type[Any]
    ) -> _ConfigSection[_ConfigSectionT] | _ConfigSectionT:
        if instance is None:
            return self
        manager = cast(UnifiedConfigManager, instance)
        return cast(_ConfigSectionT, getattr(manager.app, self._name))


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


class UnifiedConfigManager:
    """Read-only access to process configuration loaded at startup."""

    server = _ConfigSection[ServerConfig]("server")
    tasks = _ConfigSection[TaskConfig]("tasks")
    mq = _ConfigSection[MQConfig]("mq")
    security = _ConfigSection[SecurityConfig]("security")
    db = _ConfigSection[DBConfig]("db")
    redis = _ConfigSection[RedisConfig]("redis")
    noise = _ConfigSection[NoiseConfig]("noise")
    ai = _ConfigSection[AIConfig]("ai")
    notifications = _ConfigSection[NotificationConfig]("notifications")
    openclaw = _ConfigSection[OpenClawConfig]("openclaw")
    circuit_breaker = _ConfigSection[CircuitBreakerConfig]("circuit_breaker")
    retry = _ConfigSection[RetryConfig]("retry")
    maintenance = _ConfigSection[MaintenanceConfig]("maintenance")

    def __init__(self, settings: AppConfig | None = None) -> None:
        self._settings = settings

    @property
    def app(self) -> AppConfig:
        return self._settings or get_settings()
