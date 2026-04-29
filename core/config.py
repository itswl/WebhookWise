import logging
import os
import warnings as _warnings
from typing import Any

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(override=False)

_config_logger = logging.getLogger("config")


# ── 领域子配置 ──────────────────────────────────────────────


class ServerConfig(BaseSettings):
    """服务器 / 运行模式 / 日志 / 数据目录"""

    model_config = SettingsConfigDict(extra="ignore")

    WORKER_ID: str = Field(default="iMacBook-Air.local-39865")
    PORT: int = Field(default=8000)
    HOST: str = Field(default="127.0.0.1")
    METRICS_PORT: int = Field(default=0)
    DEBUG: bool = os.getenv("FLASK_ENV", "production") == "development"
    RUN_MODE: str = Field(default="all")
    ENABLE_POLLERS: bool = Field(default=True)
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="logs/webhook.log")
    DATA_DIR: str = Field(default="webhooks_data")
    ENABLE_FILE_BACKUP: bool = Field(default=False)
    JSON_SORT_KEYS: bool = Field(default=False)
    JSONIFY_PRETTYPRINT_REGULAR: bool = Field(default=True)
    MAX_CONCURRENT_WEBHOOK_TASKS: int = Field(
        default=30, description="Webhook 后台处理最大并发数（对齐 DB_POOL_SIZE + DB_MAX_OVERFLOW）"
    )
    WEBHOOK_SEMAPHORE_TIMEOUT_SECONDS: int = Field(
        default=30, description="Semaphore 获取超时秒数，超时后 Fail-Closed 放弃处理"
    )
    RECOVERY_POLLER_INTERVAL_SECONDS: int = Field(default=60, description="RecoveryPoller 扫描间隔秒数")
    RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS: int = Field(
        default=300, description="事件在 received/analyzing 状态超过此秒数视为僵尸"
    )
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS: int = Field(default=30, description="优雅停机等待正在运行任务的超时秒数")
    FORWARD_REQUEST_TIMEOUT_SECONDS: int = Field(default=10, description="单个转发请求的超时秒数")

    # Redis Stream MQ
    WEBHOOK_MQ_QUEUE: str = Field(default="webhook:queue", description="Webhook 消息队列 Redis Stream 名称")
    WEBHOOK_MQ_CONSUMER_GROUP: str = Field(default="webhook-processors", description="Consumer Group 名称")
    WEBHOOK_MQ_CONSUMER_BATCH_SIZE: int = Field(default=10, description="每次 XREADGROUP 拉取的最大消息数")
    WEBHOOK_MQ_CONSUMER_TIMEOUT_MS: int = Field(default=1000, description="XREADGROUP 阻塞超时毫秒数")
    WEBHOOK_MQ_STREAM_MAXLEN: int = Field(default=100000, description="Stream 最大长度（approximate trim）")


class SecurityConfig(BaseSettings):
    """认证 / 签名 / 限流"""

    model_config = SettingsConfigDict(extra="ignore")

    WEBHOOK_SECRET: str = Field(default="")
    API_KEY: str = Field(default="")
    ALLOW_UNAUTHENTICATED_ADMIN: bool = Field(default=False)
    MAX_WEBHOOK_BODY_BYTES: int = Field(default=1048576)
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=0)
    REQUIRE_WEBHOOK_AUTH: bool = Field(default=False, description="生产环境强制鉴权，启动时校验 WEBHOOK_SECRET")


class DBConfig(BaseSettings):
    """PostgreSQL 连接池"""

    model_config = SettingsConfigDict(extra="ignore")

    DATABASE_URL: str = Field(default="postgresql+asyncpg://postgres:postgres@localhost:5432/webhooks")
    DB_POOL_SIZE: int = Field(default=20)
    DB_MAX_OVERFLOW: int = Field(default=30)
    DB_POOL_RECYCLE: int = Field(default=3600)
    DB_POOL_TIMEOUT: int = Field(default=30)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=5000, description="SQL 语句超时(ms)")


class RedisConfig(BaseSettings):
    """Redis 连接"""

    model_config = SettingsConfigDict(extra="ignore")

    REDIS_URL: str = Field(default="redis://localhost:6379/0")


class AIConfig(BaseSettings):
    """OpenAI + AI 分析 + 提示词 + 缓存 + 降噪 + 飞书通知 + 转发"""

    model_config = SettingsConfigDict(extra="ignore")

    # AI 分析和转发
    ENABLE_AI_ANALYSIS: bool = Field(default=True)
    ENABLE_AI_DEGRADATION: bool = Field(default=False)
    FORWARD_URL: str = Field(default="")
    ENABLE_FORWARD: bool = Field(default=True)

    # OpenAI API
    OPENAI_API_KEY: str = Field(default="")
    OPENAI_API_URL: str = Field(default="https://openrouter.ai/api/v1")
    OPENAI_MODEL: str = Field(default="anthropic/claude-sonnet-4")
    OPENAI_TEMPERATURE: float = Field(default=0.2)
    OPENAI_MAX_TOKENS: int = Field(default=1800)
    OPENAI_TRUNCATION_RETRY_MAX_TOKENS: int = Field(default=2600)
    AI_CONTINUATION_ENABLED: bool = Field(
        default=True, description="是否启用 AI 响应截断自动续写，告警风暴期间可关闭以节省吞吐"
    )

    # AI 提示词
    AI_SYSTEM_PROMPT: str = Field(
        default="你是一个专业的 DevOps 和系统运维专家，擅长分析 webhook 事件并提供准确的运维建议。你的职责是：1. 快速识别事件类型和严重程度 2. 提供清晰的问题摘要 3. 给出可执行的处理建议 4. 识别潜在风险和影响范围 5. 建议监控和预防措施 重要：你必须始终返回严格符合 JSON 标准的格式，不要使用注释、尾随逗号或单引号。"
    )
    AI_USER_PROMPT_FILE: str = Field(default="prompts/webhook_analysis_detailed.txt")
    AI_USER_PROMPT: str = Field(default="")

    # 告警智能降噪 + 根因分析
    ENABLE_ALERT_NOISE_REDUCTION: bool = Field(default=True)
    NOISE_REDUCTION_WINDOW_MINUTES: int = Field(default=5)
    ROOT_CAUSE_MIN_CONFIDENCE: float = Field(default=0.65)
    SUPPRESS_DERIVED_ALERT_FORWARD: bool = Field(default=True)

    # AIOps 升级 / 缓存 / 路由
    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL: int = Field(default=21600)
    SMART_ROUTING_ENABLED: bool = Field(default=True)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015)

    # 飞书通知重要性
    IMPORTANCE_CONFIG: dict[str, Any] = Field(
        default={
            "high": {"color": "red", "emoji": "🔴", "text": "高"},
            "medium": {"color": "orange", "emoji": "🟠", "text": "中"},
            "low": {"color": "green", "emoji": "🟢", "text": "低"},
        }
    )

    # ChatOps / 飞书
    CHATOPS_ENABLED: bool = Field(default=False)
    FEISHU_BOT_APP_ID: str = Field(default="")
    FEISHU_BOT_APP_SECRET: str = Field(default="")

    # 深度分析引擎
    DEEP_ANALYSIS_ENGINE: str = Field(default="local")
    DEEP_ANALYSIS_PLATFORM: str = Field(default="openclaw")
    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default="")

    # Payload 清洗
    AI_PAYLOAD_MAX_BYTES: int = Field(default=32768, description="AI 分析输入 payload 最大字节数")
    AI_PAYLOAD_STRIP_KEYS: str = Field(
        default="images,raw_trace,stacktrace,base64_data,screenshot,binary_data",
        description="AI 分析前移除的噪音字段名，逗号分隔",
    )

    # 规则降级
    RULE_HIGH_KEYWORDS: str = Field(
        default="error,failure,critical,alert,错误,失败,故障", description="规则降级：高优先级关键字"
    )
    RULE_WARN_KEYWORDS: str = Field(default="warning,warn,警告", description="规则降级：警告级别关键字")
    RULE_METRIC_KEYWORDS: str = Field(
        default="4xxqps,5xxqps,error,cpu,memory,disk", description="规则降级：指标名称关键字"
    )
    RULE_THRESHOLD_MULTIPLIER: float = Field(default=4.0, description="规则降级：超阈值倍数提升为 high")

    # 超时
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

    # OpenClaw Device
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


class RetryConfig(BaseSettings):
    """重试 + 去重 + 周期提醒 + 通知冷却 + 锁配置 + 转发重试"""

    model_config = SettingsConfigDict(extra="ignore")

    # 重复告警去重
    DUPLICATE_ALERT_TIME_WINDOW: int = Field(default=24)
    FORWARD_DUPLICATE_ALERTS: bool = Field(default=False)
    REANALYZE_AFTER_TIME_WINDOW: bool = Field(default=True)
    FORWARD_AFTER_TIME_WINDOW: bool = Field(default=True)

    # 周期性提醒
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)
    REMINDER_INTERVAL_HOURS: int = Field(default=6)

    # 并发与通知窗口
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=120)
    PROCESSING_LOCK_WAIT_SECONDS: int = Field(default=30)
    PROCESSING_LOCK_POLL_INTERVAL_MS: int = Field(default=200)
    RECENT_BEYOND_WINDOW_REUSE_SECONDS: int = Field(default=30)
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60)

    # 保存重试
    SAVE_MAX_RETRIES: int = Field(default=3)
    SAVE_RETRY_DELAY_SECONDS: float = Field(default=0.1)

    # 转发失败重试补偿
    ENABLE_FORWARD_RETRY: bool = Field(default=True)
    FORWARD_RETRY_MAX_RETRIES: int = Field(default=3)
    FORWARD_RETRY_INITIAL_DELAY: int = Field(default=60)
    FORWARD_RETRY_MAX_DELAY: int = Field(default=3600)
    FORWARD_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0)
    FORWARD_RETRY_POLL_INTERVAL: int = Field(default=30)
    FORWARD_RETRY_BATCH_SIZE: int = Field(default=100)


# ── 顶层组合 ────────────────────────────────────────────────


class _AppConfig(BaseSettings):
    """应用配置类 — 组合 8 个领域子配置"""

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

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "_AppConfig":
        """跨字段校验，启动时自动执行。"""
        config_warnings: list[str] = []

        # 强制鉴权校验
        if self.security.REQUIRE_WEBHOOK_AUTH and not self.security.WEBHOOK_SECRET:
            raise ValueError("REQUIRE_WEBHOOK_AUTH=true 但 WEBHOOK_SECRET 为空，生产环境必须配置 WEBHOOK_SECRET")

        # 并发数与连接池对齐（警告）
        max_tasks = self.server.MAX_CONCURRENT_WEBHOOK_TASKS
        max_db = self.db.DB_POOL_SIZE + self.db.DB_MAX_OVERFLOW
        if max_tasks > max_db:
            config_warnings.append(
                f"MAX_CONCURRENT_WEBHOOK_TASKS({max_tasks}) > "
                f"DB_POOL_SIZE+DB_MAX_OVERFLOW({max_db})，"
                "应用并发数超过数据库连接池容量，建议调整"
            )

        # 非致命警告
        if not self.security.WEBHOOK_SECRET:
            config_warnings.append("WEBHOOK_SECRET 未配置，签名验证将被禁用")
        if not self.security.API_KEY:
            if self.server.DEBUG or self.security.ALLOW_UNAUTHENTICATED_ADMIN:
                config_warnings.append("API_KEY 未配置，管理接口将处于公开状态 (仅建议本地使用)")
            else:
                config_warnings.append(
                    "API_KEY 未配置，生产环境不建议启用（建议设置 API_KEY 或开启 ALLOW_UNAUTHENTICATED_ADMIN 仅用于本地）"
                )
        if self.ai.ENABLE_AI_ANALYSIS and not self.ai.OPENAI_API_KEY:
            config_warnings.append("ENABLE_AI_ANALYSIS=True 但 OPENAI_API_KEY 未配置，AI 分析将失败")
        if self.ai.ENABLE_FORWARD and not self.ai.FORWARD_URL:
            config_warnings.append("ENABLE_FORWARD=True 但 FORWARD_URL 未配置")

        if config_warnings:
            for w in config_warnings:
                _config_logger.warning("配置警告: %s", w)

        return self

    # ── 向后兼容（已废弃） ──

    _SUB_NAMES = ("server", "security", "db", "redis", "ai", "openclaw", "circuit_breaker", "retry")

    def get_flat(self, key: str, default=None):
        """[DEPRECATED] 使用 Config.子配置.字段 替代。"""
        _warnings.warn(
            f"get_flat('{key}') 已废弃，请使用层级访问 Config.<子配置>.{key}",
            DeprecationWarning,
            stacklevel=2,
        )
        for sub_name in self._SUB_NAMES:
            sub = getattr(self, sub_name)
            if hasattr(sub, key):
                return getattr(sub, key)
        return default

    def set_flat(self, key: str, value) -> bool:
        """[DEPRECATED] 使用 Config.子配置.字段 = value 替代。"""
        _warnings.warn(
            f"set_flat('{key}', ...) 已废弃，请使用层级访问 Config.<子配置>.{key} = value",
            DeprecationWarning,
            stacklevel=2,
        )
        for sub_name in self._SUB_NAMES:
            sub = getattr(self, sub_name)
            if hasattr(sub, key):
                setattr(sub, key, value)
                return True
        return False


Config = _AppConfig()
