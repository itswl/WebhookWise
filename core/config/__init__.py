"""Configuration package."""

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
from core.config.manager import ConfigKeyMeta, ConfigValueType, UnifiedConfigManager

Config = UnifiedConfigManager()

__all__ = [
    "AIConfig",
    "AppConfig",
    "CircuitBreakerConfig",
    "Config",
    "ConfigKeyMeta",
    "ConfigValueType",
    "DBConfig",
    "ForwardingConfig",
    "MaintenanceConfig",
    "MQConfig",
    "NotificationConfig",
    "OpenClawConfig",
    "RedisConfig",
    "RetryConfig",
    "SecurityConfig",
    "ServerConfig",
    "TaskConfig",
    "UnifiedConfigManager",
    "get_settings",
]
