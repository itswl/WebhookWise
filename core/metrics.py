import os

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

from core.config import Config
from core.logger import logger

# ── Source 白名单：防止 Prometheus label 基数爆炸 ─────────────────────────────
KNOWN_SOURCES: set[str] = {
    "github",
    "gitlab",
    "bitbucket",
    "cloud-monitor",
    "alert-system",
    "k8s-cluster",
    "production-server",
    "payment-system",
    "openclaw",
    "feishu-test",
    "production",
    "datadog",
    "grafana",
    "pagerduty",
    "prometheus",
    "sentry",
}


def sanitize_source(source: str) -> str:
    """将未知 source 归类为 'unknown'，防止 Prometheus 基数爆炸。

    仅用于 Prometheus label，不影响业务逻辑中的 source 值。
    """
    if not source:
        return "unknown"
    normalized = source.lower().strip()
    if normalized in KNOWN_SOURCES:
        return normalized
    return "unknown"


# 1. 业务吞吐与状态指标
WEBHOOK_RECEIVED_TOTAL = Counter("webhook_received_total", "Total number of webhooks received", ["source", "status"])

WEBHOOK_PROCESSING_STATUS_TOTAL = Counter(
    "webhook_processing_status_total",
    "Webhook processing status transitions total",
    ["status"],
)

# 2. 智能降噪指标 (AIOps 核心价值)
WEBHOOK_NOISE_REDUCED_TOTAL = Counter(
    "webhook_noise_reduced_total",
    "Number of webhooks processed by noise reduction engine",
    ["source", "relation", "suppressed"],
)

# 3. AI 成本与用量指标
AI_TOKENS_TOTAL = Counter(
    "ai_tokens_total",
    "Total number of tokens consumed by AI analysis",
    ["model", "token_type"],  # token_type: input, output
)

AI_COST_USD_TOTAL = Counter("ai_cost_usd_total", "Total estimated cost of AI analysis in USD", ["model"])

# 4. 性能指标
AI_ANALYSIS_DURATION_SECONDS = Histogram(
    "ai_analysis_duration_seconds",
    "Time spent on AI analysis",
    ["source", "engine"],
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, float("inf")),
)

WEBHOOK_PROCESSING_DURATION_SECONDS = Histogram(
    "webhook_processing_duration_seconds",
    "Time spent from start of pipeline to finish",
    ["source", "outcome"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, float("inf")),
)

OPENAI_ERRORS_TOTAL = Counter(
    "openai_errors_total",
    "OpenAI call errors total",
    ["type"],
)

# 5. 系统状态指标
DATABASE_EVENTS_COUNT = Gauge(
    "database_webhook_events_count",
    "Current number of webhook events in active table",
    multiprocess_mode="liveall",
)

# 6. 内部状态水位线指标
WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL = Counter(
    "webhook_semaphore_timeout_total",
    "Semaphore 获取超时触发 Fail-Closed 的总次数",
)

WEBHOOK_STORM_SUPPRESSED_TOTAL = Counter(
    "webhook_storm_suppressed_total",
    "告警风暴下触发 Fail-Fast 抑制的 webhook 总数",
    ["source"],
)

WEBHOOK_RECOVERY_POLLED_TOTAL = Counter(
    "webhook_recovery_polled_total",
    "RecoveryPoller 恢复的僵尸事件总数",
)

WEBHOOK_RUNNING_TASKS = Gauge(
    "webhook_running_tasks",
    "当前正在运行的 webhook 处理任务数",
    multiprocess_mode="livesum",
)

WEBHOOK_DEAD_LETTER_TOTAL = Counter(
    "webhook_dead_letter_total",
    "不可重试的死信事件总数",
)

WEBHOOK_PROCESSING_STATUS_COUNT = Gauge(
    "webhook_processing_status_count",
    "Webhook 事件各 processing_status 的数量",
    ["status"],
    multiprocess_mode="liveall",
)

WEBHOOK_STUCK_STATUS_COUNT = Gauge(
    "webhook_stuck_status_count",
    "超过阈值的僵尸事件数量（按 processing_status）",
    ["status"],
    multiprocess_mode="liveall",
)

WEBHOOK_MQ_STREAM_LENGTH = Gauge(
    "webhook_mq_stream_length",
    "Webhook Redis Stream 长度",
    ["stream"],
    multiprocess_mode="liveall",
)

WEBHOOK_MQ_GROUP_PENDING = Gauge(
    "webhook_mq_group_pending",
    "Webhook Redis Stream consumer group pending 数量",
    ["stream", "group"],
    multiprocess_mode="liveall",
)

WEBHOOK_MQ_GROUP_LAG = Gauge(
    "webhook_mq_group_lag",
    "Webhook Redis Stream consumer group lag 数量",
    ["stream", "group"],
    multiprocess_mode="liveall",
)

DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "当前已借出的数据库连接数",
    multiprocess_mode="livesum",
)

DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "数据库连接池总容量（pool_size + max_overflow）",
    multiprocess_mode="liveall",
)


def update_db_pool_metrics():
    """更新数据库连接池容量指标。

    checked_out 已通过 Pool 事件回调实时更新（见 db/session.py），
    此函数仅更新连接池容量上限（极少变化）。
    """
    try:
        from db.session import get_engine

        engine = get_engine()
        if engine is None:
            return
        pool = engine.sync_engine.pool
        DB_POOL_SIZE.set(pool.size() + pool.overflow())
    except (AttributeError, RuntimeError) as e:
        logger.warning("[Metrics] 无法获取 DB 连接池容量: %s", e)


class _DBPoolCollector:
    """Prometheus 自定义 Collector，在每次 scrape 时触发 DB 连接池指标更新。"""

    def describe(self):
        return []

    def collect(self):
        update_db_pool_metrics()
        return []


prometheus_client.REGISTRY.register(_DBPoolCollector())


def setup_metrics(app):
    """初始化并挂载 Prometheus 指标。

    自动检测 PROMETHEUS_MULTIPROC_DIR 环境变量：
    - 已设置：多进程模式，使用 MultiProcessCollector 聚合各 Worker 指标
    - 未设置：单进程模式，保持 Instrumentator 原有行为
    """
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")

    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_instrument_requests_inprogress=True,
        excluded_handlers=[".*admin.*", "/metrics", "/health", "/"],
        inprogress_name="webhook_requests_inprogress",
        inprogress_labels=True,
    )
    instrumentator.instrument(app)

    if multiproc_dir:
        # ── 多进程模式：Gunicorn 多 Worker 聚合 ──
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            generate_latest,
            multiprocess,
        )
        from starlette.responses import Response

        logger.info("[Metrics] Prometheus 多进程模式: %s", multiproc_dir)

        async def metrics_endpoint(request):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            data = generate_latest(registry)
            return Response(content=data, media_type=CONTENT_TYPE_LATEST)

        app.add_route("/metrics", metrics_endpoint, methods=["GET"])
    elif Config.server.METRICS_PORT > 0 and Config.server.METRICS_PORT != Config.server.PORT:
        # ── 单进程 + 独立端口模式 ──
        try:
            prometheus_client.start_http_server(port=Config.server.METRICS_PORT, addr=Config.server.HOST)
            logger.info(f"[Metrics] 成功启动独立的 Prometheus 监控端口: {Config.server.METRICS_PORT}")
        except OSError as e:
            if "Address already in use" in str(e):
                logger.debug(
                    f"[Metrics] 独立监控端口 {Config.server.METRICS_PORT} 已被其他 Worker 绑定，复用该指标服务。"
                )
            else:
                logger.error(f"[Metrics] 启动独立监控端口失败: {e}")
        # 注意：无论是否绑定成功，都不向主 FastAPI 挂载 /metrics 路由，实现端口隔离。
    else:
        # ── 单进程 + 复用主端口模式 ──
        logger.info(f"[Metrics] 复用主程序端口暴露 Prometheus 监控: {Config.server.PORT}/metrics")
        instrumentator.expose(app, endpoint="/metrics")

    return instrumentator
