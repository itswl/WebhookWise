"""Configuration package — re-exports for backward compatibility."""

from core.config.defaults import (
    AIConfig,
    AppConfig,
    CircuitBreakerConfig,
    DBConfig,
    MaintenanceConfig,
    OpenClawConfig,
    RedisConfig,
    RetryConfig,
    RuntimeType,
    RuntimeValue,
    SecurityConfig,
    ServerConfig,
    get_settings,
)
from core.config.runtime import UnifiedConfigManager

Config = UnifiedConfigManager()

__all__ = [
    "AIConfig",
    "AppConfig",
    "CircuitBreakerConfig",
    "Config",
    "DBConfig",
    "MaintenanceConfig",
    "OpenClawConfig",
    "RedisConfig",
    "RetryConfig",
    "RuntimeType",
    "RuntimeValue",
    "SecurityConfig",
    "ServerConfig",
    "UnifiedConfigManager",
    "get_settings",
]
