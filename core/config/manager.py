"""Static configuration facade backed by ``core.config.defaults.get_settings``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, TypedDict, get_args, get_origin

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


class UnifiedConfigManager:
    """Read-only access to process configuration loaded at startup."""

    def __init__(self, settings: AppConfig | None = None) -> None:
        self._settings = settings

    @property
    def app(self) -> AppConfig:
        return self._settings or get_settings()

    @property
    def server(self) -> ServerConfig:
        return self.app.server

    @property
    def tasks(self) -> TaskConfig:
        return self.app.tasks

    @property
    def mq(self) -> MQConfig:
        return self.app.mq

    @property
    def security(self) -> SecurityConfig:
        return self.app.security

    @property
    def db(self) -> DBConfig:
        return self.app.db

    @property
    def redis(self) -> RedisConfig:
        return self.app.redis

    @property
    def noise(self) -> NoiseConfig:
        return self.app.noise

    @property
    def ai(self) -> AIConfig:
        return self.app.ai

    @property
    def notifications(self) -> NotificationConfig:
        return self.app.notifications

    @property
    def openclaw(self) -> OpenClawConfig:
        return self.app.openclaw

    @property
    def circuit_breaker(self) -> CircuitBreakerConfig:
        return self.app.circuit_breaker

    @property
    def retry(self) -> RetryConfig:
        return self.app.retry

    @property
    def maintenance(self) -> MaintenanceConfig:
        return self.app.maintenance
