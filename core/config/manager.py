"""Static configuration facade backed by ``core.config.defaults.get_settings``."""

from __future__ import annotations

from typing import Literal, TypedDict, get_args, get_origin

from pydantic_settings import BaseSettings

from core.config.defaults import (
    AIConfig,
    AppConfig,
    CircuitBreakerConfig,
    DBConfig,
    ForwardingConfig,
    MaintenanceConfig,
    MQConfig,
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


def _build_config_keys() -> dict[str, ConfigKeyMeta]:
    settings = get_settings()
    config_keys: dict[str, ConfigKeyMeta] = {}
    for sub_name in settings._SUB_NAMES:
        sub_config: BaseSettings = getattr(settings, sub_name)
        for key, field in type(sub_config).model_fields.items():
            value_type = _config_type_for_annotation(field.annotation)
            if value_type is None:
                continue
            config_keys[key] = {"type": value_type, "sub": sub_name}
    return config_keys


class UnifiedConfigManager:
    """Read-only access to process configuration loaded at startup."""

    CONFIG_KEYS: dict[str, ConfigKeyMeta] = _build_config_keys()

    @property
    def server(self) -> ServerConfig:
        return get_settings().server

    @property
    def tasks(self) -> TaskConfig:
        return get_settings().tasks

    @property
    def mq(self) -> MQConfig:
        return get_settings().mq

    @property
    def security(self) -> SecurityConfig:
        return get_settings().security

    @property
    def db(self) -> DBConfig:
        return get_settings().db

    @property
    def redis(self) -> RedisConfig:
        return get_settings().redis

    @property
    def ai(self) -> AIConfig:
        return get_settings().ai

    @property
    def forwarding(self) -> ForwardingConfig:
        return get_settings().forwarding

    @property
    def notifications(self) -> NotificationConfig:
        return get_settings().notifications

    @property
    def openclaw(self) -> OpenClawConfig:
        return get_settings().openclaw

    @property
    def circuit_breaker(self) -> CircuitBreakerConfig:
        return get_settings().circuit_breaker

    @property
    def retry(self) -> RetryConfig:
        return get_settings().retry

    @property
    def maintenance(self) -> MaintenanceConfig:
        return get_settings().maintenance

    @property
    def app(self) -> AppConfig:
        return get_settings()
