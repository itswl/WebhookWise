"""Pydantic settings definitions — static configuration from env / .env files."""

import os
import socket
from functools import lru_cache
from typing import Literal, TypeAlias

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)

RuntimeType: TypeAlias = Literal["str", "int", "float", "bool"]
RuntimeValue: TypeAlias = str | int | float | bool


class ServerConfig(BaseSettings):
    """服务器 / 运行模式 / 日志 / 数据目录"""

    model_config = SettingsConfigDict(extra="ignore")

    WORKER_ID: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    PORT: int = Field(default=8000)
    HOST: str = Field(default="127.0.0.1")
    METRICS_PORT: int = Field(default=0)
    DEBUG: bool = os.getenv("APP_ENV", "production") == "development"
    RUN_MODE: str = Field(default="api")
    ENABLE_RUNTIME_CONFIG: bool = os.getenv("APP_ENV", "production") == "development"
    ALLOW_RUNTIME_CONNECTION_CONFIG: bool = Field(default=False)
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="logs/webhook.log")
    DATA_DIR: str = Field(default="webhooks_data")
    RECOVERY_POLLER_INTERVAL_SECONDS: int = Field(default=60)
    RECOVERY_SCAN_INTERVAL_SECONDS: int = Field(default=300)
    METRICS_REFRESH_INTERVAL_SECONDS: int = Field(default=60)
    RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS: int = Field(default=300)
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS: int = Field(default=30)
    FORWARD_REQUEST_TIMEOUT_SECONDS: int = Field(default=10)
    PAYLOAD_OFFLOAD_THRESHOLD_BYTES: int = Field(default=524288)
    MAX_CONCURRENT_WEBHOOK_TASKS: int = Field(default=30)
    WEBHOOK_TASK_SLOT_LEASE_SECONDS: int = Field(default=1800)

    WEBHOOK_MQ_QUEUE: str = Field(default="webhook:queue")
    WEBHOOK_MQ_CONSUMER_GROUP: str = Field(default="webhook-processors")
    WEBHOOK_MQ_CONSUMER_BATCH_SIZE: int = Field(default=10)
    WEBHOOK_MQ_CONSUMER_TIMEOUT_MS: int = Field(default=1000)
    WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS: int = Field(default=300000)
    WEBHOOK_MQ_STREAM_MAXLEN: int = Field(default=100000)


class SecurityConfig(BaseSettings):
    """认证 / 签名 / 限流"""

    model_config = SettingsConfigDict(extra="ignore")

    WEBHOOK_SECRET: str = Field(default="")
    API_KEY: str = Field(default="")
    ADMIN_WRITE_KEY: str = Field(default="")
    ALLOW_UNAUTHENTICATED_ADMIN: bool = Field(default=False)
    ALLOW_UNAUTHENTICATED_WEBHOOK: bool = Field(default=False)
    MAX_WEBHOOK_BODY_BYTES: int = Field(default=1048576)
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=0)
    WEBHOOK_RATE_LIMIT_BURST: int = Field(default=0)
    WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE: int = Field(default=0)
    REQUIRE_WEBHOOK_AUTH: bool = Field(default=False)
    TRUST_PROXY_HEADERS: bool = Field(default=False)
    TRUSTED_PROXY_CIDRS: str = Field(default="127.0.0.1/32,::1/128")
    ALLOW_PRIVATE_FORWARD_URLS: bool = Field(default=False)
    FORWARD_TARGET_ALLOWLIST: str = Field(default="")


class DBConfig(BaseSettings):
    """PostgreSQL 连接池"""

    model_config = SettingsConfigDict(extra="ignore")

    DATABASE_URL: str = Field(default="postgresql+asyncpg://postgres:postgres@localhost:5432/webhooks")
    DB_POOL_SIZE: int = Field(default=5)
    DB_MAX_OVERFLOW: int = Field(default=5)
    DB_POOL_RECYCLE: int = Field(default=3600)
    DB_POOL_TIMEOUT: int = Field(default=30)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=30000)
    DB_SYNC_COMMIT: str = Field(default="off")


class RedisConfig(BaseSettings):
    """Redis 连接"""

    model_config = SettingsConfigDict(extra="ignore")

    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    REDIS_SOCKET_CONNECT_TIMEOUT: int = Field(default=5)
    REDIS_SOCKET_TIMEOUT: int = Field(default=10)
    REDIS_HEALTH_CHECK_INTERVAL: int = Field(default=30)


class AIConfig(BaseSettings):
    """OpenAI + AI 分析 + 降噪"""

    model_config = SettingsConfigDict(extra="ignore")

    ENABLE_AI_ANALYSIS: bool = Field(default=True)
    FORWARD_URL: str = Field(default="")
    ENABLE_FORWARD: bool = Field(default=True)
    OPENAI_API_KEY: str = Field(default="")
    OPENAI_API_URL: str = Field(default="https://openrouter.ai/api/v1")
    OPENAI_MODEL: str = Field(default="anthropic/claude-sonnet-4")
    AI_SYSTEM_PROMPT: str = Field(default="你是一个专业的 DevOps 和系统运维专家...")
    ENABLE_ALERT_NOISE_REDUCTION: bool = Field(default=True)
    NOISE_REDUCTION_WINDOW_MINUTES: int = Field(default=5)
    ROOT_CAUSE_MIN_CONFIDENCE: float = Field(default=0.65)
    NOISE_RELATED_MIN_CONFIDENCE: float = Field(default=0.35)
    NOISE_SOURCE_WEIGHT: float = Field(default=0.15)
    NOISE_RESOURCE_WEIGHT: float = Field(default=0.45)
    NOISE_SEMANTIC_WEIGHT: float = Field(default=0.25)
    NOISE_SEVERITY_WEIGHT: float = Field(default=0.10)
    NOISE_TIME_WEIGHT: float = Field(default=0.20)
    NOISE_SEVERITY_DOWNGRADE_SCORE: float = Field(default=0.03)
    SUPPRESS_DERIVED_ALERT_FORWARD: bool = Field(default=True)
    AI_PAYLOAD_MAX_BYTES: int = Field(default=32768)
    AI_PAYLOAD_STRIP_KEYS: str = Field(default="images,raw_trace,stacktrace,base64_data,screenshot,binary_data")
    RULE_HIGH_KEYWORDS: str = Field(default="error,failure,critical,alert,错误,失败,故障")
    RULE_WARN_KEYWORDS: str = Field(default="warning,warn,警告")
    RULE_METRIC_KEYWORDS: str = Field(default="4xxqps,5xxqps,error,cpu,memory,disk")
    RULE_THRESHOLD_MULTIPLIER: float = Field(default=4.0)

    ENABLE_AI_DEGRADATION: bool = Field(default=False)
    OPENAI_TEMPERATURE: float = Field(default=0.2)
    AI_USER_PROMPT_FILE: str = Field(default="prompts/webhook_analysis_detailed.txt")
    AI_USER_PROMPT: str = Field(default="")

    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL: int = Field(default=21600)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015)

    DEEP_ANALYSIS_PLATFORM: str = Field(default="openclaw")
    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default="")
    FEISHU_WEBHOOK_TIMEOUT: int = Field(default=10)
    FORWARD_TIMEOUT: int = Field(default=10)


class OpenClawConfig(BaseSettings):
    """OpenClaw 深度分析引擎"""

    model_config = SettingsConfigDict(extra="ignore")

    OPENCLAW_ENABLED: bool = Field(default=False)
    OPENCLAW_GATEWAY_URL: str = Field(default="http://127.0.0.1:18900")
    OPENCLAW_GATEWAY_TOKEN: str = Field(default="")
    OPENCLAW_HOOKS_TOKEN: str = Field(default="")
    OPENCLAW_HTTP_API_URL: str = Field(default="http://127.0.0.1:8085")
    OPENCLAW_TIMEOUT_SECONDS: int = Field(default=900)
    OPENCLAW_STABILITY_REQUIRED_HITS: int = Field(default=2)
    OPENCLAW_POLL_INITIAL_DELAY_SECONDS: int = Field(default=10)
    OPENCLAW_POLL_MAX_DELAY_SECONDS: int = Field(default=120)
    OPENCLAW_POLL_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    OPENCLAW_MAX_CONSECUTIVE_ERRORS: int = Field(default=8)
    OPENCLAW_ENABLE_DEGRADATION: bool = Field(default=False)
    OPENCLAW_CONNECT_TIMEOUT: int = Field(default=20)
    OPENCLAW_HANDSHAKE_TIMEOUT: int = Field(default=10)
    OPENCLAW_NONCE_TIMEOUT: float = Field(default=5.0)
    OPENCLAW_POLL_TIMEOUT: int = Field(default=180)
    OPENCLAW_DEVICE_ID: str = Field(default="")
    OPENCLAW_DEVICE_PRIVATE_KEY_PEM: str = Field(default="")
    OPENCLAW_DEVICE_TOKEN: str = Field(default="")


class CircuitBreakerConfig(BaseSettings):
    """熔断器"""

    model_config = SettingsConfigDict(extra="ignore")

    CIRCUIT_BREAKER_FEISHU_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FEISHU_TIMEOUT: float = Field(default=30.0)
    CIRCUIT_BREAKER_OPENCLAW_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_OPENCLAW_TIMEOUT: float = Field(default=30.0)
    CIRCUIT_BREAKER_FORWARD_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FORWARD_TIMEOUT: float = Field(default=30.0)


class MaintenanceConfig(BaseSettings):
    """数据清理 / 归档 / 维护"""

    model_config = SettingsConfigDict(extra="ignore")

    ENABLE_ARCHIVE_CLEANUP: bool = Field(default=True)
    ARCHIVE_DAYS_DEFAULT: int = Field(default=30)
    RETENTION_POLICIES: dict[str, int] = Field(default={"high": 90, "medium": 30, "low": 7, "unknown": 3})
    SOURCE_RETENTION_POLICIES: dict[str, int] = Field(default={"prometheus": 30, "grafana": 30, "datadog": 30})
    CLEANUP_KEYWORDS: dict[str, list[str]] = Field(
        default={"summary": ["一般事件:", "测试告警"], "parsed_data": ["一般事件"]}
    )
    MAINTENANCE_HOUR: int = Field(default=3)


class RetryConfig(BaseSettings):
    """重试 + 去重 + 周期提醒"""

    model_config = SettingsConfigDict(extra="ignore")

    DUPLICATE_ALERT_TIME_WINDOW: int = Field(default=24)
    FORWARD_DUPLICATE_ALERTS: bool = Field(default=False)
    REANALYZE_AFTER_TIME_WINDOW: bool = Field(default=True)
    FORWARD_AFTER_TIME_WINDOW: bool = Field(default=True)
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)
    REMINDER_INTERVAL_HOURS: int = Field(default=6)
    PROCESSING_LOCK_DISTRIBUTED_ENABLED: bool = Field(default=True)
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=180)
    PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS: int = Field(default=15)
    PROCESSING_LOCK_POLL_INTERVAL_MS: int = Field(default=100)
    PROCESSING_LOCK_FAILFAST_THRESHOLD: int = Field(default=20)
    PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS: int = Field(default=10)
    RECENT_BEYOND_WINDOW_REUSE_SECONDS: int = Field(default=30)
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60)
    WEBHOOK_RETRY_MAX_RETRIES: int = Field(default=5)
    WEBHOOK_RETRY_INITIAL_DELAY: int = Field(default=30)
    WEBHOOK_RETRY_MAX_DELAY: int = Field(default=900)
    WEBHOOK_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    ENABLE_FORWARD_RETRY: bool = Field(default=True)
    FORWARD_RETRY_MAX_RETRIES: int = Field(default=3)
    FORWARD_RETRY_INITIAL_DELAY: int = Field(default=60)
    FORWARD_RETRY_MAX_DELAY: int = Field(default=3600)
    FORWARD_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)


class AppConfig(BaseSettings):
    """应用配置类 — 组合所有领域子配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    db: DBConfig = Field(default_factory=DBConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    openclaw: OpenClawConfig = Field(default_factory=OpenClawConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    _SUB_NAMES: tuple[str, ...] = (
        "server",
        "security",
        "db",
        "redis",
        "ai",
        "openclaw",
        "circuit_breaker",
        "retry",
        "maintenance",
    )

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "AppConfig":
        if self.security.REQUIRE_WEBHOOK_AUTH and not self.security.WEBHOOK_SECRET:
            raise ValueError("REQUIRE_WEBHOOK_AUTH=true 但 WEBHOOK_SECRET 为空")
        return self


@lru_cache
def get_settings() -> AppConfig:
    return AppConfig()
