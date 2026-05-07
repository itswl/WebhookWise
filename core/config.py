import logging
import os
import socket
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal, TypeAlias, TypedDict, TypeVar

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)

_config_logger = logging.getLogger("config")

RuntimeType: TypeAlias = Literal["str", "int", "float", "bool"]
RuntimeValue: TypeAlias = str | int | float | bool


class _RuntimeKeyMeta(TypedDict):
    type: RuntimeType
    sub: str


_TSubSettings = TypeVar("_TSubSettings", bound=BaseSettings)


# ── 领域子配置 ──────────────────────────────────────────────


class ServerConfig(BaseSettings):
    """服务器 / 运行模式 / 日志 / 数据目录"""

    model_config = SettingsConfigDict(extra="ignore")

    WORKER_ID: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    PORT: int = Field(default=8000)
    HOST: str = Field(default="127.0.0.1")
    METRICS_PORT: int = Field(default=0)
    DEBUG: bool = os.getenv("APP_ENV", "production") == "development"
    RUN_MODE: str = Field(default="all")
    ENABLE_POLLERS: bool = Field(default=True)
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="logs/webhook.log")
    DATA_DIR: str = Field(default="webhooks_data")
    ENABLE_FILE_BACKUP: bool = Field(default=False)
    JSON_SORT_KEYS: bool = Field(default=False)
    JSONIFY_PRETTYPRINT_REGULAR: bool = Field(default=True)
    MAX_CONCURRENT_WEBHOOK_TASKS: int = Field(default=30)
    WEBHOOK_SEMAPHORE_TIMEOUT_SECONDS: int = Field(default=30)
    RECOVERY_POLLER_INTERVAL_SECONDS: int = Field(default=60)
    RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS: int = Field(default=300)
    RECOVERY_POLLER_CONCURRENCY: int = Field(default=5)
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS: int = Field(default=30)
    FORWARD_REQUEST_TIMEOUT_SECONDS: int = Field(default=10)
    PAYLOAD_OFFLOAD_THRESHOLD_BYTES: int = Field(default=524288)

    WEBHOOK_MQ_QUEUE: str = Field(default="webhook:queue")
    WEBHOOK_MQ_CONSUMER_GROUP: str = Field(default="webhook-processors")
    WEBHOOK_MQ_CONSUMER_BATCH_SIZE: int = Field(default=10)
    WEBHOOK_MQ_CONSUMER_TIMEOUT_MS: int = Field(default=1000)
    WEBHOOK_MQ_STREAM_MAXLEN: int = Field(default=100000)
    MQ_CONSUMER_CONCURRENCY: int = Field(default=10)


class SecurityConfig(BaseSettings):
    """认证 / 签名 / 限流"""

    model_config = SettingsConfigDict(extra="ignore")

    WEBHOOK_SECRET: str = Field(default="")
    API_KEY: str = Field(default="")
    ADMIN_WRITE_KEY: str = Field(default="")
    ALLOW_UNAUTHENTICATED_ADMIN: bool = Field(default=False)
    MAX_WEBHOOK_BODY_BYTES: int = Field(default=1048576)
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=0)
    REQUIRE_WEBHOOK_AUTH: bool = Field(default=False)


class DBConfig(BaseSettings):
    """PostgreSQL 连接池"""

    model_config = SettingsConfigDict(extra="ignore")

    DATABASE_URL: str = Field(default="postgresql+asyncpg://postgres:postgres@localhost:5432/webhooks")
    DB_POOL_SIZE: int = Field(default=20)
    DB_MAX_OVERFLOW: int = Field(default=30)
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
    SUPPRESS_DERIVED_ALERT_FORWARD: bool = Field(default=True)
    AI_PAYLOAD_MAX_BYTES: int = Field(default=32768)
    AI_PAYLOAD_STRIP_KEYS: str = Field(default="images,raw_trace,stacktrace,base64_data,screenshot,binary_data")
    RULE_HIGH_KEYWORDS: str = Field(default="error,failure,critical,alert,错误,失败,故障")
    RULE_WARN_KEYWORDS: str = Field(default="warning,warn,警告")
    RULE_METRIC_KEYWORDS: str = Field(default="4xxqps,5xxqps,error,cpu,memory,disk")
    RULE_THRESHOLD_MULTIPLIER: float = Field(default=4.0)

    ENABLE_AI_DEGRADATION: bool = Field(default=False)
    OPENAI_TEMPERATURE: float = Field(default=0.2)
    OPENAI_MAX_TOKENS: int = Field(default=1800)
    OPENAI_TRUNCATION_RETRY_MAX_TOKENS: int = Field(default=2600)
    AI_CONTINUATION_ENABLED: bool = Field(default=True)
    AI_USER_PROMPT_FILE: str = Field(default="prompts/webhook_analysis_detailed.txt")
    AI_USER_PROMPT: str = Field(default="")

    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL: int = Field(default=21600)
    SMART_ROUTING_ENABLED: bool = Field(default=True)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015)

    IMPORTANCE_CONFIG: dict[str, Any] = Field(
        default={
            "high": {"color": "red", "emoji": "🔴", "text": "高"},
            "medium": {"color": "orange", "emoji": "🟠", "text": "中"},
            "low": {"color": "green", "emoji": "🟢", "text": "低"},
        }
    )

    CHATOPS_ENABLED: bool = Field(default=False)
    FEISHU_BOT_APP_ID: str = Field(default="")
    FEISHU_BOT_APP_SECRET: str = Field(default="")
    DEEP_ANALYSIS_ENGINE: str = Field(default="local")
    DEEP_ANALYSIS_PLATFORM: str = Field(default="openclaw")
    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default="")
    AI_API_TIMEOUT: int = Field(default=10)
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
    OPENCLAW_TIMEOUT_SECONDS: int = Field(default=300)
    OPENCLAW_STABILITY_REQUIRED_HITS: int = Field(default=2)
    OPENCLAW_MIN_WAIT_SECONDS: int = Field(default=30)
    OPENCLAW_MAX_CONSECUTIVE_ERRORS: int = Field(default=5)
    OPENCLAW_ENABLE_DEGRADATION: bool = Field(default=False)
    OPENCLAW_CONNECT_TIMEOUT: int = Field(default=10)
    OPENCLAW_HANDSHAKE_TIMEOUT: int = Field(default=5)
    OPENCLAW_RECV_TIMEOUT: float = Field(default=1.0)
    OPENCLAW_NONCE_TIMEOUT: float = Field(default=2.0)
    OPENCLAW_POLL_TIMEOUT: int = Field(default=90)
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
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=120)
    PROCESSING_LOCK_WAIT_SECONDS: int = Field(default=30)
    PROCESSING_LOCK_POLL_INTERVAL_MS: int = Field(default=200)
    PROCESSING_LOCK_FAILFAST_THRESHOLD: int = Field(default=20)
    PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS: int = Field(default=10)
    PROCESSING_LOCK_STORM_KEEP_LATEST_N: int = Field(default=200)
    RECENT_BEYOND_WINDOW_REUSE_SECONDS: int = Field(default=30)
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60)
    SAVE_MAX_RETRIES: int = Field(default=3)
    SAVE_RETRY_DELAY_SECONDS: float = Field(default=0.1)
    ENABLE_FORWARD_RETRY: bool = Field(default=True)
    FORWARD_RETRY_MAX_RETRIES: int = Field(default=3)
    FORWARD_RETRY_INITIAL_DELAY: int = Field(default=60)
    FORWARD_RETRY_MAX_DELAY: int = Field(default=3600)
    FORWARD_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    FORWARD_RETRY_POLL_INTERVAL: int = Field(default=30)
    FORWARD_RETRY_BATCH_SIZE: int = Field(default=100)
    FORWARD_RETRY_CONCURRENCY: int = Field(default=10)


# ── 顶层组合与统一管理器 ────────────────────────────────────────────────


class _AppConfig(BaseSettings):
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
    def _validate_cross_fields(self) -> "_AppConfig":
        if self.security.REQUIRE_WEBHOOK_AUTH and not self.security.WEBHOOK_SECRET:
            raise ValueError("REQUIRE_WEBHOOK_AUTH=true 但 WEBHOOK_SECRET 为空")
        return self


@lru_cache
def get_settings() -> _AppConfig:
    return _AppConfig()


class _UnifiedConfigManager:
    """统一配置管理器：只提供静态配置读取（环境变量 / .env / 默认值）。"""

    # 公开暴露给管理接口读取的配置键（不支持运行时热更新）
    RUNTIME_KEYS: dict[str, _RuntimeKeyMeta] = {
        "FORWARD_URL": {"type": "str", "sub": "ai"},
        "ENABLE_FORWARD": {"type": "bool", "sub": "ai"},
        "ENABLE_AI_ANALYSIS": {"type": "bool", "sub": "ai"},
        "OPENAI_API_KEY": {"type": "str", "sub": "ai"},
        "OPENAI_API_URL": {"type": "str", "sub": "ai"},
        "OPENAI_MODEL": {"type": "str", "sub": "ai"},
        "AI_SYSTEM_PROMPT": {"type": "str", "sub": "ai"},
        "LOG_LEVEL": {"type": "str", "sub": "server"},
        "DUPLICATE_ALERT_TIME_WINDOW": {"type": "int", "sub": "retry"},
        "FORWARD_DUPLICATE_ALERTS": {"type": "bool", "sub": "retry"},
        "REANALYZE_AFTER_TIME_WINDOW": {"type": "bool", "sub": "retry"},
        "FORWARD_AFTER_TIME_WINDOW": {"type": "bool", "sub": "retry"},
        "ENABLE_ALERT_NOISE_REDUCTION": {"type": "bool", "sub": "ai"},
        "NOISE_REDUCTION_WINDOW_MINUTES": {"type": "int", "sub": "ai"},
        "ROOT_CAUSE_MIN_CONFIDENCE": {"type": "float", "sub": "ai"},
        "SUPPRESS_DERIVED_ALERT_FORWARD": {"type": "bool", "sub": "ai"},
        "RULE_HIGH_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_WARN_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_METRIC_KEYWORDS": {"type": "str", "sub": "ai"},
        "RULE_THRESHOLD_MULTIPLIER": {"type": "float", "sub": "ai"},
        "AI_PAYLOAD_MAX_BYTES": {"type": "int", "sub": "ai"},
        "AI_PAYLOAD_STRIP_KEYS": {"type": "str", "sub": "ai"},
        "NOTIFICATION_COOLDOWN_SECONDS": {"type": "int", "sub": "retry"},
        "ENABLE_PERIODIC_REMINDER": {"type": "bool", "sub": "retry"},
        "REMINDER_INTERVAL_HOURS": {"type": "int", "sub": "retry"},
    }

    def __init__(self) -> None:
        self._overrides: dict[str, RuntimeValue] = {}
        self._meta: dict[str, dict[str, object]] = {}

    def _merged_sub(self, sub_name: str, base: _TSubSettings) -> _TSubSettings:
        updates: dict[str, RuntimeValue] = {}
        for key, value in self._overrides.items():
            meta = self.RUNTIME_KEYS.get(key)
            if not meta or meta["sub"] != sub_name:
                continue
            if not hasattr(base, key):
                continue
            updates[key] = value
        if not updates:
            return base
        return base.model_copy(update=updates)

    @property
    def server(self) -> ServerConfig:
        return self._merged_sub("server", get_settings().server)

    @property
    def security(self) -> SecurityConfig:
        return self._merged_sub("security", get_settings().security)

    @property
    def db(self) -> DBConfig:
        return self._merged_sub("db", get_settings().db)

    @property
    def redis(self) -> RedisConfig:
        return self._merged_sub("redis", get_settings().redis)

    @property
    def ai(self) -> AIConfig:
        return self._merged_sub("ai", get_settings().ai)

    @property
    def openclaw(self) -> OpenClawConfig:
        return self._merged_sub("openclaw", get_settings().openclaw)

    @property
    def circuit_breaker(self) -> CircuitBreakerConfig:
        return self._merged_sub("circuit_breaker", get_settings().circuit_breaker)

    @property
    def retry(self) -> RetryConfig:
        return self._merged_sub("retry", get_settings().retry)

    @property
    def maintenance(self) -> MaintenanceConfig:
        return self._merged_sub("maintenance", get_settings().maintenance)

    def get_meta(self, key: str) -> dict[str, object]:
        return self._meta.get(key, {})

    def set_override(
        self,
        key: str,
        value: RuntimeValue | None,
        *,
        source: str = "override",
        updated_by: str | None = None,
    ) -> None:
        if value is None:
            self._overrides.pop(key, None)
            self._meta.pop(key, None)
        else:
            self._overrides[key] = value
            self._meta[key] = {"source": source, "updated_at": datetime.now(), "updated_by": updated_by}
        if key == "LOG_LEVEL":
            level = getattr(logging, str(value or "INFO").upper(), logging.INFO)
            logging.getLogger("webhook_service").setLevel(level)

    async def load_from_db(self) -> bool:
        return False

    async def start_subscriber(self) -> None:
        return

    async def stop_subscriber(self) -> None:
        return

    async def save_batch(self, updates: dict[str, RuntimeValue], updated_by: str = "api") -> None:
        raise RuntimeError("运行时配置写入已禁用")


Config = _UnifiedConfigManager()
policies = Config  # 保持向下兼容
