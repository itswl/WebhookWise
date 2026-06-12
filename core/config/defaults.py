"""Pydantic settings definitions — static configuration from env / .env files."""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StaticSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ServerConfig(StaticSettings):
    """服务器 / 运行模式 / 日志"""

    APP_ENV: str = Field(default="production")
    WORKER_ID: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    PORT: int = Field(default=8000)
    HOST: str = Field(default="127.0.0.1")
    DEBUG: bool = Field(default=False)
    RUN_MODE: str = Field(default="api")
    LOG_LEVEL: str = Field(default="INFO")
    THIRD_PARTY_LOG_LEVEL: str = Field(default="WARNING")
    PAYLOAD_OFFLOAD_THRESHOLD_BYTES: int = Field(default=524288)
    PAYLOAD_COMPRESS_THRESHOLD_BYTES: int = Field(default=4096)
    PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES: int = Field(default=4096)



class TaskConfig(StaticSettings):
    """TaskIQ worker/runtime scheduling."""

    BACKGROUND_SCAN_INTERVAL_SECONDS: int = Field(default=300)
    METRICS_REFRESH_INTERVAL_SECONDS: int = Field(default=60)
    FORWARD_OUTBOX_STALE_SECONDS: int = Field(default=300, description="Outbox 记录认领后超时秒数; 需大于 FORWARD_TIMEOUT + 退避上限，否则正常重试的记录会被误判为过期")
    WORKER_STARTUP_JITTER_SECONDS: float = Field(default=0.0)


class MQConfig(StaticSettings):
    """Webhook Redis Stream queue."""

    WEBHOOK_MQ_QUEUE: str = Field(default="webhook:queue")
    WEBHOOK_MQ_CONSUMER_GROUP: str = Field(default="webhook-processors")
    WEBHOOK_MQ_CONSUMER_BATCH_SIZE: int = Field(default=10)
    WEBHOOK_MQ_CONSUMER_TIMEOUT_MS: int = Field(default=1000)
    WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS: int = Field(default=300000)
    WEBHOOK_MQ_STREAM_MAXLEN: int = Field(default=100000)
    # Priority queue: ingest tasks for high-severity alerts are routed here so a
    # backlog of low-priority AI analysis on the default queue cannot starve P0
    # alerts. A dedicated priority worker pool consumes it.
    WEBHOOK_MQ_PRIORITY_QUEUE: str = Field(default="webhook:queue:priority")
    WEBHOOK_MQ_PRIORITY_CONSUMER_GROUP: str = Field(default="webhook-processors-priority")
    # Enable severity-based priority routing at ingest. When false, everything
    # goes to the default queue (current behavior).
    WEBHOOK_PRIORITY_ROUTING_ENABLED: bool = Field(default=False)
    # Normalized levels (adapters.normalize_level output) routed to the priority
    # queue. Comma-separated; default just "critical".
    WEBHOOK_PRIORITY_LEVELS: str = Field(default="critical")


class SecurityConfig(StaticSettings):
    """认证 / 签名 / 限流"""

    WEBHOOK_SECRET: str = Field(default="")
    API_KEY: str = Field(default="")
    ADMIN_WRITE_KEY: str = Field(default="")
    MAX_WEBHOOK_BODY_BYTES: int = Field(default=1048576)
    HSTS_INCLUDE_SUBDOMAINS: bool = Field(default=False)
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=0)
    WEBHOOK_RATE_LIMIT_BURST: int = Field(default=0)
    WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE: int = Field(default=0)
    RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR: bool = Field(default=False, description="true: Redis 不可用时降级放行; false(默认): 拒绝请求返回 503。生产环境面向公网时建议 false 以防止限流失效")
    REQUIRE_WEBHOOK_AUTH: bool = Field(default=True)
    WEBHOOK_REPLAY_PROTECTION_ENABLED: bool = Field(
        default=False,
        description="true: 对带签名的 webhook 强制校验时间戳+nonce 防重放(需上游发送 x-webhook-timestamp)。默认 false 保持向后兼容",
    )
    WEBHOOK_REPLAY_MAX_SKEW_SECONDS: int = Field(default=300, description="签名时间戳允许的最大时钟偏差(秒)")
    TRUST_PROXY_HEADERS: bool = Field(default=False)
    TRUSTED_PROXY_CIDRS: str = Field(default="127.0.0.1/32,::1/128")
    ALLOW_PRIVATE_TARGET_URLS: bool = Field(default=False)
    FORWARD_TARGET_ALLOWLIST: str = Field(default="")


class DBConfig(StaticSettings):
    """PostgreSQL 连接池"""

    DATABASE_URL: str
    # Per-process pool. Total Postgres connections ≈ (API workers + worker
    # procs) × (DB_POOL_SIZE + DB_MAX_OVERFLOW). Size deliberately against
    # Postgres max_connections and expected per-request concurrency; consider
    # pgbouncer when scaling out. Defaults suit a small single-node deployment.
    DB_POOL_SIZE: int = Field(default=5, description="每进程连接池常驻连接数")
    DB_MAX_OVERFLOW: int = Field(default=5, description="每进程连接池可临时超出的连接数")
    DB_POOL_RECYCLE: int = Field(default=3600)
    DB_POOL_TIMEOUT: int = Field(default=30, description="等待空闲连接的超时(秒);超时请求会报错")
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=30000)
    DB_SYNC_COMMIT: str = Field(default="on")


def _db_config_factory() -> DBConfig:
    return DBConfig()  # type: ignore[call-arg]


class RedisConfig(StaticSettings):
    """Redis 连接"""

    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    REDIS_SOCKET_CONNECT_TIMEOUT: int = Field(default=5)
    REDIS_SOCKET_TIMEOUT: int = Field(default=10)
    REDIS_HEALTH_CHECK_INTERVAL: int = Field(default=30)


class NoiseConfig(StaticSettings):
    """告警降噪参数"""

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


class AIConfig(StaticSettings):
    """OpenAI + AI 分析"""

    ENABLE_AI_ANALYSIS: bool = Field(default=True)
    OPENAI_API_KEY: str = Field(default="")
    OPENAI_API_URL: str = Field(default="https://openrouter.ai/api/v1")
    OPENAI_MODEL: str = Field(default="anthropic/claude-sonnet-4")
    # instructor structured-output mode (case-insensitive Mode name). "json" is
    # the safe default; set a stricter schema mode when the upstream provider
    # supports it for fewer malformed outputs at the source — e.g.
    # "openrouter_structured_outputs" (OpenRouter), "tools_strict"/"json_schema"
    # (OpenAI). Unknown/unsupported names fall back to JSON at client init.
    AI_INSTRUCTOR_MODE: str = Field(default="json", description="instructor 结构化输出模式名(不区分大小写)")
    AI_SYSTEM_PROMPT: str = Field(default="你是一个专业的 DevOps 和系统运维专家...")
    AI_HTTP_TIMEOUT_SECONDS: float = Field(default=60.0)
    AI_HTTP_CONNECT_TIMEOUT_SECONDS: float = Field(default=10.0)
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
    DEEP_ANALYSIS_PROMPT_FILE: str = Field(default="prompts/deep_analysis.txt")
    DEEP_ANALYSIS_PROMPT: str = Field(default="")

    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL: int = Field(default=21600)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015)

    DEEP_ANALYSIS_PLATFORM: str = Field(default="openclaw")


class NotificationConfig(StaticSettings):
    """Feishu and operational notification settings."""

    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default="")
    FEISHU_WEBHOOK_TIMEOUT: int = Field(default=10)
    AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=3600)


class OpenClawConfig(StaticSettings):
    """OpenClaw 深度分析引擎"""

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
    OPENCLAW_NONCE_TIMEOUT: float = Field(default=5.0)
    OPENCLAW_POLL_TIMEOUT: int = Field(default=180)
    OPENCLAW_POLL_STABILITY_TTL_SECONDS: int = Field(default=3600)
    OPENCLAW_WS_MAX_HISTORY_FRAMES: int = Field(default=50)
    OPENCLAW_DEVICE_ID: str = Field(default="")
    OPENCLAW_DEVICE_PRIVATE_KEY_PEM: str = Field(default="")
    OPENCLAW_DEVICE_TOKEN: str = Field(default="")


class CircuitBreakerConfig(StaticSettings):
    """熔断器"""

    CIRCUIT_BREAKER_FEISHU_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FEISHU_TIMEOUT: float = Field(default=30.0)
    CIRCUIT_BREAKER_OPENCLAW_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_OPENCLAW_TIMEOUT: float = Field(default=30.0)
    CIRCUIT_BREAKER_FORWARD_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FORWARD_TIMEOUT: float = Field(default=30.0)
    # LLM (main AI analysis) breaker: when the provider is broadly failing, open
    # the breaker so each alert degrades to rule analysis immediately instead of
    # paying the full retry+timeout budget per webhook.
    CIRCUIT_BREAKER_LLM_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_LLM_TIMEOUT: float = Field(default=30.0)


class MaintenanceConfig(StaticSettings):
    """数据清理 / 保留策略 / 维护"""

    ENABLE_DATA_CLEANUP: bool = Field(default=True)
    DATA_RETENTION_DAYS_DEFAULT: int = Field(default=30)
    RETENTION_POLICIES: dict[str, int] = Field(default={"high": 90, "medium": 30, "low": 7, "unknown": 3})
    SOURCE_RETENTION_POLICIES: dict[str, int] = Field(default={"prometheus": 30, "grafana": 30, "datadog": 30})
    CLEANUP_KEYWORDS: dict[str, list[str]] = Field(
        default={"summary": ["一般事件:", "测试告警"], "parsed_data": ["一般事件"]}
    )
    MAINTENANCE_HOUR: int = Field(default=3)


class RetryConfig(StaticSettings):
    """重试 + 去重 + 周期提醒"""

    DEDUP_WINDOW_SECONDS: int = Field(default=14400)
    ANALYSIS_REUSE_WINDOW_SECONDS: int = Field(default=43200)
    FORWARD_DUPLICATE_ALERTS: bool = Field(default=False)
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)
    REMINDER_INTERVAL_HOURS: int = Field(default=6)
    PROCESSING_LOCK_DISTRIBUTED_ENABLED: bool = Field(default=True)
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=180)
    PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS: int = Field(default=15)
    PROCESSING_LOCK_POLL_INTERVAL_MS: int = Field(default=100)
    PROCESSING_LOCK_FAILFAST_THRESHOLD: int = Field(default=20)
    PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS: int = Field(default=10)
    INGRESS_BACKPRESSURE_FAIL_OPEN_ON_REDIS_ERROR: bool = Field(default=True, description="Redis 不可用时背压检查是否放行; true: 降级放行, false: 拒绝请求")
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60)
    WEBHOOK_RETRY_MAX_RETRIES: int = Field(default=5)
    WEBHOOK_RETRY_INITIAL_DELAY: int = Field(default=30)
    WEBHOOK_RETRY_MAX_DELAY: int = Field(default=900)
    WEBHOOK_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    FORWARD_RETRY_MAX_RETRIES: int = Field(default=3)
    FORWARD_RETRY_INITIAL_DELAY: int = Field(default=60)
    FORWARD_RETRY_MAX_DELAY: int = Field(default=3600)
    FORWARD_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    FORWARD_MAX_DELIVERY_AGE_SECONDS: int = Field(default=1800)
    FORWARD_TIMEOUT: int = Field(default=10)


class AppConfig(StaticSettings):
    """应用配置类 — 组合所有领域子配置"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    mq: MQConfig = Field(default_factory=MQConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    db: DBConfig = Field(default_factory=_db_config_factory)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    openclaw: OpenClawConfig = Field(default_factory=OpenClawConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    _SUB_NAMES: tuple[str, ...] = (
        "server",
        "tasks",
        "mq",
        "security",
        "db",
        "redis",
        "noise",
        "ai",
        "notifications",
        "openclaw",
        "circuit_breaker",
        "retry",
        "maintenance",
    )



@lru_cache
def get_settings() -> AppConfig:
    return AppConfig()
