"""Pydantic settings definitions — static configuration from env / .env files."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.worker_identity import default_worker_id


class StaticSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ServerConfig(StaticSettings):
    """Server / run mode / logging."""

    APP_ENV: str = Field(default="production")
    WORKER_ID: str = Field(default_factory=default_worker_id)
    PORT: int = Field(default=8000, ge=1, le=65535)
    DEBUG: bool = Field(default=False)
    RUN_MODE: Literal["api", "worker", "scheduler", "migrate"] = Field(default="api")
    LOG_LEVEL: str = Field(default="INFO")
    THIRD_PARTY_LOG_LEVEL: str = Field(default="WARNING")
    PAYLOAD_OFFLOAD_THRESHOLD_BYTES: int = Field(default=524288, ge=0)
    PAYLOAD_COMPRESS_THRESHOLD_BYTES: int = Field(default=4096, ge=0)
    PAYLOAD_DECOMPRESS_ASYNC_THRESHOLD_BYTES: int = Field(default=4096, ge=0)


class TaskConfig(StaticSettings):
    """TaskIQ worker/runtime scheduling."""

    BACKGROUND_SCAN_INTERVAL_SECONDS: int = Field(default=300, gt=0)
    METRICS_REFRESH_INTERVAL_SECONDS: int = Field(default=60, gt=0)
    FORWARD_OUTBOX_STALE_SECONDS: int = Field(
        default=300,
        gt=0,
        description="Seconds before an actively processing outbox record may be reclaimed; must exceed one delivery timeout",
    )
    WORKER_STARTUP_JITTER_SECONDS: float = Field(default=0.0, ge=0.0)
    TASKIQ_RESULT_TTL_SECONDS: int = Field(
        # Almost every task here is fire-and-forget (webhook processing, outbox
        # delivery, periodic scans) — nothing awaits the stored result, so a
        # long retention only accumulates dead keys and AOF volume (observed:
        # ~139k result keys under the previous 24h default). One hour keeps
        # results inspectable for debugging without the buildup.
        default=3600,
        gt=0,
        description="Seconds to retain TaskIQ task results in Redis before automatic expiry",
    )
    TASKIQ_SCHEDULE_REDIS_URL: str = Field(
        default="",
        description="Optional Redis URL dedicated to dynamic schedules; defaults to REDIS_URL with the database incremented",
    )
    TASKIQ_SCHEDULE_SCAN_BUFFER_SIZE: int = Field(
        default=1000,
        gt=0,
        description="Redis SCAN batch size for dynamic schedules",
    )


class MQConfig(StaticSettings):
    """Webhook Redis Stream queue."""

    WEBHOOK_MQ_QUEUE: str = Field(default="webhook:queue")
    WEBHOOK_MQ_CONSUMER_GROUP: str = Field(default="webhook-processors")
    WEBHOOK_MQ_CONSUMER_BATCH_SIZE: int = Field(default=10, gt=0)
    WEBHOOK_MQ_CONSUMER_TIMEOUT_MS: int = Field(default=1000, gt=0)
    WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS: int = Field(default=300000, gt=0)
    WEBHOOK_MQ_STREAM_MAXLEN: int = Field(default=100000, gt=0)
    # Fraction of MAXLEN at which the queue is flagged as backlogged in the
    # Action Center / dashboard (visibility only; no request is rejected). 0
    # disables the warning.
    WEBHOOK_MQ_BACKLOG_WARN_FRACTION: float = Field(default=0.8, ge=0.0, le=1.0)
    # Fraction of MAXLEN above which ingress applies backpressure: the request
    # is rejected with 503 so the (retrying) upstream holds it, instead of
    # letting the stream silently trim its oldest un-acked entries. 0 disables
    # (default), because enabling request rejection is an ops decision that
    # should follow MAXLEN capacity planning and confirmed upstream retries.
    WEBHOOK_MQ_INGRESS_HIGH_WATER_FRACTION: float = Field(default=0.0, ge=0.0, le=1.0)


class SecurityConfig(StaticSettings):
    """Authentication / signing / rate limiting."""

    WEBHOOK_SECRET: str = Field(default="")
    API_KEY: str = Field(default="")
    ADMIN_WRITE_KEY: str = Field(default="")
    MAX_WEBHOOK_BODY_BYTES: int = Field(default=1048576, gt=0)
    HSTS_INCLUDE_SUBDOMAINS: bool = Field(default=False)
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=0, ge=0)
    WEBHOOK_RATE_LIMIT_BURST: int = Field(default=0, ge=0)
    WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE: int = Field(default=0, ge=0)
    ADMIN_API_RATE_LIMIT_PER_MINUTE: int = Field(
        default=300,
        ge=0,
        description="Per-IP per-minute rate limit for the authenticated admin/read API; 0 disables it. It throttles API Key brute force and load and must remain higher than the number of requests in a single Dashboard load",
    )
    ADMIN_ACTION_COOLDOWN_SECONDS: int = Field(
        default=60,
        ge=0,
        description="Per-resource cooldown for expensive operator actions such as re-analysis, manual forwarding, and dead-letter replay; 0 disables it",
    )
    RATE_LIMIT_FAIL_OPEN_ON_REDIS_ERROR: bool = Field(
        default=False,
        description="true: degrade to allow when Redis is unavailable; false (default): reject the request with 503. For public-facing production, false is recommended to prevent rate limiting from being bypassed",
    )
    REQUIRE_WEBHOOK_AUTH: bool = Field(default=True)
    WEBHOOK_REPLAY_PROTECTION_ENABLED: bool = Field(
        default=False,
        description="true: enforce timestamp + nonce replay protection for signed webhooks (requires the upstream to send x-webhook-timestamp). Defaults to false to preserve backward compatibility",
    )
    WEBHOOK_REPLAY_MAX_SKEW_SECONDS: int = Field(
        default=300, gt=0, description="Maximum allowed clock skew for the signature timestamp (seconds)"
    )
    TRUST_PROXY_HEADERS: bool = Field(default=False)
    TRUSTED_PROXY_CIDRS: str = Field(default="127.0.0.1/32,::1/128")
    ALLOW_PRIVATE_TARGET_URLS: bool = Field(default=False)
    FORWARD_TARGET_ALLOWLIST: str = Field(default="")
    MCP_ENABLED: bool = Field(
        default=False,
        description="Expose the read-only MCP server at /mcp. Off by default; requires API_KEY and MCP_ALLOWED_HOSTS to be set when serving behind a reverse proxy",
    )
    MCP_ALLOWED_HOSTS: str = Field(
        default="",
        description="Comma-separated Host header values allowed for the /mcp endpoint (DNS-rebinding protection). localhost/127.0.0.1 are always allowed; add the public host when behind a reverse proxy, e.g. 'dejavu.example.com,dejavu.example.com:443'. Empty = loopback only",
    )
    MCP_ALLOWED_ORIGINS: str = Field(
        default="",
        description="Comma-separated Origin header values allowed for the /mcp endpoint; empty = loopback origins only",
    )


class DBConfig(StaticSettings):
    """PostgreSQL connection pool."""

    DATABASE_URL: str
    # Per-process pool. Total Postgres connections ≈ (API workers + worker
    # procs) × (DB_POOL_SIZE + DB_MAX_OVERFLOW). Size deliberately against
    # Postgres max_connections and expected per-request concurrency; consider
    # pgbouncer when scaling out. Defaults suit a small single-node deployment.
    DB_POOL_SIZE: int = Field(default=5, ge=1, description="Number of persistent connections in the per-process pool")
    DB_MAX_OVERFLOW: int = Field(
        default=5, ge=0, description="Number of connections the per-process pool may temporarily exceed by"
    )
    DB_POOL_RECYCLE_SECONDS: int = Field(default=3600, gt=0)
    DB_POOL_TIMEOUT_SECONDS: int = Field(
        default=30,
        gt=0,
        description="Timeout (seconds) for waiting on an idle connection; requests that time out raise an error",
    )
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=30000, gt=0)
    DB_SYNC_COMMIT: Literal["on", "off", "local", "remote_write", "remote_apply"] = Field(default="on")


def _db_config_factory() -> DBConfig:
    return DBConfig()  # type: ignore[call-arg]


class RedisConfig(StaticSettings):
    """Redis connection."""

    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS: int = Field(default=5, gt=0)
    REDIS_SOCKET_TIMEOUT_SECONDS: int = Field(default=10, gt=0)
    REDIS_HEALTH_CHECK_INTERVAL_SECONDS: int = Field(default=30, gt=0)


class NoiseConfig(StaticSettings):
    """Alert noise-reduction parameters."""

    ENABLE_ALERT_NOISE_REDUCTION: bool = Field(default=True)
    NOISE_REDUCTION_WINDOW_MINUTES: int = Field(default=5, gt=0)
    ROOT_CAUSE_MIN_CONFIDENCE: float = Field(default=0.65, ge=0.0, le=1.0)
    NOISE_RELATED_MIN_CONFIDENCE: float = Field(default=0.35, ge=0.0, le=1.0)
    NOISE_SOURCE_WEIGHT: float = Field(default=0.15, ge=0.0, le=1.0)
    NOISE_RESOURCE_WEIGHT: float = Field(default=0.45, ge=0.0, le=1.0)
    NOISE_SEMANTIC_WEIGHT: float = Field(default=0.25, ge=0.0, le=1.0)
    NOISE_SEVERITY_WEIGHT: float = Field(default=0.10, ge=0.0, le=1.0)
    NOISE_TIME_WEIGHT: float = Field(default=0.20, ge=0.0, le=1.0)
    NOISE_SEVERITY_DOWNGRADE_SCORE: float = Field(default=0.03, ge=0.0, le=1.0)
    SUPPRESS_DERIVED_ALERT_FORWARD: bool = Field(default=True)

    # Status flapping (Nagios sense): one alert identity oscillating
    # firing↔recovered. Detection is always on and cheap (a Redis flip window
    # per identity); it feeds the Action Center "currently flapping" item and
    # the decision trace. Suppressing notifications while an identity flaps is
    # OPT-IN — enabling it withholds both the firing and the recovery cards
    # until the identity stays quiet for a full window.
    FLAPPING_WINDOW_MINUTES: int = Field(default=10, gt=0)
    FLAPPING_MIN_TRANSITIONS: int = Field(default=6, gt=0)
    FLAPPING_SUPPRESS_ENABLED: bool = Field(default=False)

    @model_validator(mode="after")
    def validate_confidence_order(self) -> NoiseConfig:
        if self.ROOT_CAUSE_MIN_CONFIDENCE < self.NOISE_RELATED_MIN_CONFIDENCE:
            raise ValueError("ROOT_CAUSE_MIN_CONFIDENCE must be at least NOISE_RELATED_MIN_CONFIDENCE")
        return self


class AIConfig(StaticSettings):
    """OpenAI + AI analysis."""

    ENABLE_AI_ANALYSIS: bool = Field(default=True)
    OPENAI_API_KEY: str = Field(default="")
    OPENAI_API_URL: str = Field(default="https://openrouter.ai/api/v1")
    OPENAI_MODEL: str = Field(default="anthropic/claude-sonnet-4")
    # instructor structured-output mode (case-insensitive Mode name). "json" is
    # the safe default; set a stricter schema mode when the upstream provider
    # supports it for fewer malformed outputs at the source — e.g.
    # "openrouter_structured_outputs" (OpenRouter), "tools_strict"/"json_schema"
    # (OpenAI). Unknown/unsupported names fall back to JSON at client init.
    AI_INSTRUCTOR_MODE: str = Field(
        default="json", description="instructor structured-output mode name (case-insensitive)"
    )
    AI_SYSTEM_PROMPT: str = Field(default="你是一个专业的 DevOps 和系统运维专家...")
    AI_HTTP_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0.0)
    AI_HTTP_CONNECT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0.0)
    AI_PAYLOAD_MAX_BYTES: int = Field(default=32768, gt=0)
    AI_PAYLOAD_STRIP_KEYS: str = Field(default="images,raw_trace,stacktrace,base64_data,screenshot,binary_data")
    RULE_HIGH_KEYWORDS: str = Field(default="error,failure,critical,alert,错误,失败,故障")
    RULE_WARN_KEYWORDS: str = Field(default="warning,warn,警告")
    RULE_METRIC_KEYWORDS: str = Field(default="4xxqps,5xxqps,error,cpu,memory,disk")
    RULE_THRESHOLD_MULTIPLIER: float = Field(default=4.0, gt=0.0)

    # Tiered AI routing: when on, alerts the rule pass judges low-value skip the
    # (paid) LLM and return the rule analysis directly, concentrating AI spend on
    # alerts that need it. OFF by default → behavior unchanged. AI_ROUTING_SKIP_
    # IMPORTANCE lists the rule-importances that bypass the LLM (default "low").
    AI_ROUTING_ENABLED: bool = Field(default=False)
    AI_ROUTING_SKIP_IMPORTANCE: str = Field(default="low")

    ENABLE_AI_DEGRADATION: bool = Field(default=True)
    OPENAI_TEMPERATURE: float = Field(default=0.2, ge=0.0, le=2.0)
    AI_USER_PROMPT_FILE: str = Field(default="prompts/webhook_analysis_detailed.txt")
    AI_USER_PROMPT: str = Field(default="")
    DEEP_ANALYSIS_PROMPT_FILE: str = Field(default="prompts/deep_analysis.txt")
    DEEP_ANALYSIS_PROMPT: str = Field(default="")
    INCIDENT_SUMMARY_PROMPT_FILE: str = Field(default="prompts/incident_summary.txt")
    INCIDENT_SUMMARY_PROMPT: str = Field(default="")

    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL_SECONDS: int = Field(default=21600, gt=0)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003, ge=0.0)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015, ge=0.0)

    DEEP_ANALYSIS_PLATFORM: str = Field(default="openclaw")


class KBConfig(StaticSettings):
    """RAG knowledge base: inject relevant internal docs into AI analysis.

    Off by default. When KB_ENABLED and documents exist, each alert's analysis
    retrieves the top-K most similar knowledge chunks and folds them into the
    prompt as reference context. Embeddings come from a dedicated endpoint
    (KB_EMBEDDING_*); when none is configured a deterministic local placeholder
    embedding is used so the whole pipeline runs/tests end to end without an
    external service (placeholder retrieval quality is low — configure a real
    endpoint for semantic matching).
    """

    KB_ENABLED: bool = Field(default=False)
    # Dedicated embeddings endpoint. Blank → falls back to the local placeholder
    # embedding. Configured separately from the main OPENAI_API_URL so the
    # embedding model can differ from the chat model, but it may point at the same
    # provider (e.g. OpenRouter exposes /embeddings like qwen/qwen3-embedding-8b,
    # so the main key can be reused). Set these to switch to real semantic
    # retrieval, and set KB_VECTOR_DIM to the model's native dimension.
    KB_EMBEDDING_API_URL: str = Field(default="")
    KB_EMBEDDING_API_KEY: str = Field(default="")
    KB_EMBEDDING_MODEL: str = Field(default="text-embedding-3-small")
    # Must match the configured embedding model's output dimension (placeholder
    # uses this too). E.g. 256 placeholder / 1536 text-embedding-3-small / 4096
    # qwen3-embedding-8b. Mismatched dimensions are treated as no match.
    KB_VECTOR_DIM: int = Field(default=256, gt=0)
    KB_TOP_K: int = Field(default=3, gt=0)
    KB_MIN_SCORE: float = Field(default=0.3, ge=-1.0, le=1.0)
    KB_MAX_CANDIDATES: int = Field(default=1000, gt=0, le=10000)
    KB_CHUNK_MAX_CHARS: int = Field(default=800, gt=0)
    KB_MAX_CONTEXT_CHARS: int = Field(default=2000, gt=0)
    KB_EMBEDDING_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)

    # Attach matching published KB entries to outgoing Feishu alert cards (a
    # small "runbook" block, cheap token matching, no LLM call). Independent of
    # KB_ENABLED, which gates RAG context in the AI prompt; with no published
    # documents the block simply never appears.
    KB_CARD_LINKS_ENABLED: bool = Field(default=True)
    KB_CARD_LINKS_MAX: int = Field(default=2, gt=0, le=5)


class NotificationConfig(StaticSettings):
    """Feishu and operational notification settings."""

    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default="")
    FEISHU_WEBHOOK_TIMEOUT_SECONDS: int = Field(default=10, gt=0)
    AI_ERROR_NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=3600, gt=0)
    DASHBOARD_PUBLIC_URL: str = Field(default="")

    # Periodic alert-health digest (cost + noise report). Reads already-collected
    # AIUsageLog + webhook_events, summarizes the numbers, and pushes one card to
    # the report webhook (each cadence falls back to WEEKLY_REPORT_FEISHU_WEBHOOK,
    # then DEEP_ANALYSIS_FEISHU_WEBHOOK). All off by default; enable any cadence to
    # get "are my alerts healthy / where did AI $ go" over that window.
    # Cron is evaluated in the container timezone (Asia/Shanghai in the image).
    WEEKLY_REPORT_ENABLED: bool = Field(default=False)
    WEEKLY_REPORT_CRON: str = Field(default="0 9 * * 1")  # Monday 09:00
    WEEKLY_REPORT_WINDOW_DAYS: int = Field(default=7, gt=0)
    WEEKLY_REPORT_FEISHU_WEBHOOK: str = Field(default="")

    DAILY_REPORT_ENABLED: bool = Field(default=False)
    DAILY_REPORT_CRON: str = Field(default="0 9 * * *")  # every day 09:00
    DAILY_REPORT_WINDOW_DAYS: int = Field(default=1, gt=0)
    DAILY_REPORT_FEISHU_WEBHOOK: str = Field(default="")
    DAILY_REPORT_ONLY_ON_ACTIVITY: bool = Field(
        default=True,
        description="Skip the daily report when its window has no alerts, AI calls, or operator actions",
    )

    MONTHLY_REPORT_ENABLED: bool = Field(default=False)
    MONTHLY_REPORT_CRON: str = Field(default="0 9 1 * *")  # 1st of month 09:00
    MONTHLY_REPORT_WINDOW_DAYS: int = Field(default=30, gt=0)
    MONTHLY_REPORT_FEISHU_WEBHOOK: str = Field(default="")

    # AI cost budget alert: when the current calendar month's accumulated AI
    # spend crosses AI_COST_MONTHLY_BUDGET_USD * AI_COST_BUDGET_ALERT_THRESHOLD,
    # push one Feishu card (checked by the daily report task, at most once per
    # month per crossing). 0 = disabled. Webhook falls back to the daily report
    # webhook, then the deep-analysis webhook.
    AI_COST_MONTHLY_BUDGET_USD: float = Field(default=0.0, ge=0.0)
    AI_COST_BUDGET_ALERT_THRESHOLD: float = Field(default=0.8, gt=0.0, le=1.0)  # warn at 80% of budget
    AI_COST_BUDGET_FEISHU_WEBHOOK: str = Field(default="")

    # Escalation-lite: arm each new incident's SLA from its importance
    # ("high=30,medium=240" → a high incident unacknowledged for 30 minutes
    # triggers the SLA-breach escalation card). Empty = off (default): SLAs
    # stay operator-set only. Clearing an armed SLA on a still-firing incident
    # re-arms it when the next member alert lands.
    INCIDENT_AUTO_SLA_MINUTES: str = Field(default="")
    # Make the breach card louder: @all mention, and/or a dedicated escalation
    # webhook (falls back to the deep-analysis, then weekly-report webhook).
    SLA_BREACH_MENTION_ALL: bool = Field(default=False)
    SLA_BREACH_FEISHU_WEBHOOK: str = Field(default="")


class OpenClawConfig(StaticSettings):
    """OpenClaw deep-analysis engine."""

    OPENCLAW_ENABLED: bool = Field(default=False)
    OPENCLAW_GATEWAY_URL: str = Field(default="http://127.0.0.1:18900")
    OPENCLAW_GATEWAY_TOKEN: str = Field(default="")
    OPENCLAW_HOOKS_TOKEN: str = Field(default="")
    OPENCLAW_HTTP_API_URL: str = Field(default="http://127.0.0.1:8085")
    OPENCLAW_TIMEOUT_SECONDS: int = Field(default=900, gt=0)
    OPENCLAW_STABILITY_REQUIRED_HITS: int = Field(default=2, gt=0)
    OPENCLAW_POLL_INITIAL_DELAY_SECONDS: int = Field(default=10, gt=0)
    OPENCLAW_POLL_MAX_DELAY_SECONDS: int = Field(default=120, gt=0)
    OPENCLAW_POLL_BACKOFF_MULTIPLIER: float = Field(default=2.0, ge=1.0)
    OPENCLAW_MAX_CONSECUTIVE_ERRORS: int = Field(default=8, gt=0)
    OPENCLAW_ENABLE_DEGRADATION: bool = Field(default=False)
    OPENCLAW_CONNECT_TIMEOUT_SECONDS: int = Field(default=20, gt=0)
    OPENCLAW_NONCE_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0.0)
    OPENCLAW_POLL_TIMEOUT_SECONDS: int = Field(default=180, gt=0)
    OPENCLAW_POLL_STABILITY_TTL_SECONDS: int = Field(default=3600, gt=0)
    OPENCLAW_WS_MAX_HISTORY_FRAMES: int = Field(default=50, gt=0)
    OPENCLAW_DEVICE_ID: str = Field(default="")
    OPENCLAW_DEVICE_PRIVATE_KEY_PEM: str = Field(default="")
    OPENCLAW_DEVICE_TOKEN: str = Field(default="")


class CircuitBreakerConfig(StaticSettings):
    """Circuit breaker."""

    CIRCUIT_BREAKER_FEISHU_THRESHOLD: int = Field(default=5, gt=0)
    CIRCUIT_BREAKER_FEISHU_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)
    CIRCUIT_BREAKER_OPENCLAW_THRESHOLD: int = Field(default=5, gt=0)
    CIRCUIT_BREAKER_OPENCLAW_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)
    CIRCUIT_BREAKER_FORWARD_THRESHOLD: int = Field(default=5, gt=0)
    CIRCUIT_BREAKER_FORWARD_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)
    # LLM (main AI analysis) breaker: when the provider is broadly failing, open
    # the breaker so each alert degrades to rule analysis immediately instead of
    # paying the full retry+timeout budget per webhook.
    CIRCUIT_BREAKER_LLM_THRESHOLD: int = Field(default=5, gt=0)
    CIRCUIT_BREAKER_LLM_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)


class MaintenanceConfig(StaticSettings):
    """Data cleanup / retention policy / maintenance."""

    ENABLE_DATA_CLEANUP: bool = Field(default=True)
    DATA_RETENTION_DAYS_DEFAULT: int = Field(default=30, gt=0)
    RETENTION_POLICIES: dict[str, int] = Field(default={"high": 90, "medium": 30, "low": 7, "unknown": 3})
    SOURCE_RETENTION_POLICIES: dict[str, int] = Field(default={"prometheus": 30, "grafana": 30, "datadog": 30})
    CLEANUP_KEYWORDS: dict[str, list[str]] = Field(
        default={"summary": ["一般事件:", "测试告警"], "parsed_data": ["一般事件"]}
    )
    ARCHIVE_RETENTION_DAYS: int = Field(default=90, gt=0)
    TERMINAL_OUTBOX_RETENTION_DAYS: int = Field(default=30, gt=0)
    AI_USAGE_RETENTION_DAYS: int = Field(default=90, gt=0)
    INCIDENT_AUTO_CLOSE_DAYS: int = Field(default=7, gt=0)
    MAINTENANCE_HOUR: int = Field(default=3, ge=0, le=23)


class RetryConfig(StaticSettings):
    """Retries + deduplication + periodic reminders."""

    DEDUP_WINDOW_SECONDS: int = Field(default=14400, gt=0)
    ANALYSIS_REUSE_WINDOW_SECONDS: int = Field(default=43200, gt=0)
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)
    REMINDER_INTERVAL_HOURS: int = Field(default=6, gt=0)
    PROCESSING_LOCK_DISTRIBUTED_ENABLED: bool = Field(default=True)
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=180, gt=0)
    PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS: int = Field(default=15, ge=0)
    PROCESSING_LOCK_POLL_INTERVAL_MS: int = Field(default=100, gt=0)
    PROCESSING_LOCK_FAILFAST_THRESHOLD: int = Field(default=20, ge=0)
    PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS: int = Field(default=10, gt=0)
    INGRESS_BACKPRESSURE_FAIL_OPEN_ON_REDIS_ERROR: bool = Field(
        default=False,
        description="Whether the backpressure check allows requests when Redis is unavailable; true: degrade to allow, false: reject the request",
    )
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60, ge=0)
    WEBHOOK_RETRY_MAX_RETRIES: int = Field(default=5, ge=0)
    WEBHOOK_RETRY_INITIAL_DELAY_SECONDS: int = Field(default=30, gt=0)
    WEBHOOK_RETRY_MAX_DELAY_SECONDS: int = Field(default=900, gt=0)
    WEBHOOK_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0, ge=1.0)
    FORWARD_RETRY_MAX_RETRIES: int = Field(default=3, ge=0)
    FORWARD_RETRY_INITIAL_DELAY_SECONDS: int = Field(default=60, gt=0)
    FORWARD_RETRY_MAX_DELAY_SECONDS: int = Field(default=3600, gt=0)
    FORWARD_RETRY_BACKOFF_MULTIPLIER: float = Field(default=2.0, ge=1.0)
    FORWARD_MAX_DELIVERY_AGE_SECONDS: int = Field(default=1800, ge=0)
    FORWARD_TIMEOUT_SECONDS: int = Field(default=10, gt=0)


class AppConfig(StaticSettings):
    """Application configuration class — composes all domain sub-configs."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    mq: MQConfig = Field(default_factory=MQConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    db: DBConfig = Field(default_factory=_db_config_factory)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    openclaw: OpenClawConfig = Field(default_factory=OpenClawConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    @model_validator(mode="after")
    def validate_runtime_relationships(self) -> AppConfig:
        if self.ai.AI_HTTP_CONNECT_TIMEOUT_SECONDS > self.ai.AI_HTTP_TIMEOUT_SECONDS:
            raise ValueError("AI_HTTP_CONNECT_TIMEOUT_SECONDS must not exceed AI_HTTP_TIMEOUT_SECONDS")
        if self.retry.ANALYSIS_REUSE_WINDOW_SECONDS < self.retry.DEDUP_WINDOW_SECONDS:
            raise ValueError("ANALYSIS_REUSE_WINDOW_SECONDS must be at least DEDUP_WINDOW_SECONDS")
        if self.retry.WEBHOOK_RETRY_INITIAL_DELAY_SECONDS > self.retry.WEBHOOK_RETRY_MAX_DELAY_SECONDS:
            raise ValueError("WEBHOOK_RETRY_INITIAL_DELAY_SECONDS must not exceed WEBHOOK_RETRY_MAX_DELAY_SECONDS")
        if self.retry.FORWARD_RETRY_INITIAL_DELAY_SECONDS > self.retry.FORWARD_RETRY_MAX_DELAY_SECONDS:
            raise ValueError("FORWARD_RETRY_INITIAL_DELAY_SECONDS must not exceed FORWARD_RETRY_MAX_DELAY_SECONDS")
        if self.tasks.FORWARD_OUTBOX_STALE_SECONDS <= self.retry.FORWARD_TIMEOUT_SECONDS:
            raise ValueError("FORWARD_OUTBOX_STALE_SECONDS must exceed FORWARD_TIMEOUT_SECONDS")
        if self.openclaw.OPENCLAW_POLL_INITIAL_DELAY_SECONDS > self.openclaw.OPENCLAW_POLL_MAX_DELAY_SECONDS:
            raise ValueError("OPENCLAW_POLL_INITIAL_DELAY_SECONDS must not exceed OPENCLAW_POLL_MAX_DELAY_SECONDS")
        if self.openclaw.OPENCLAW_CONNECT_TIMEOUT_SECONDS > self.openclaw.OPENCLAW_TIMEOUT_SECONDS:
            raise ValueError("OPENCLAW_CONNECT_TIMEOUT_SECONDS must not exceed OPENCLAW_TIMEOUT_SECONDS")
        if self.kb.KB_TOP_K > self.kb.KB_MAX_CANDIDATES:
            raise ValueError("KB_TOP_K must not exceed KB_MAX_CANDIDATES")
        return self


@lru_cache
def get_settings() -> AppConfig:
    return AppConfig()
