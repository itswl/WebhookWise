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

# 5. 系统状态指标
DATABASE_EVENTS_COUNT = Gauge("database_webhook_events_count", "Current number of webhook events in active table")

# 6. 内部状态水位线指标
WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL = Counter(
    "webhook_semaphore_timeout_total",
    "Semaphore 获取超时触发 Fail-Closed 的总次数",
)

WEBHOOK_RECOVERY_POLLED_TOTAL = Counter(
    "webhook_recovery_polled_total",
    "RecoveryPoller 恢复的僵尸事件总数",
)

WEBHOOK_RUNNING_TASKS = Gauge(
    "webhook_running_tasks",
    "当前正在运行的 webhook 处理任务数",
)

DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "当前已借出的数据库连接数",
)

DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "数据库连接池总容量（pool_size + max_overflow）",
)


def update_db_pool_metrics():
    """更新数据库连接池指标。

    在 Prometheus scrape 时通过自定义 Collector 自动调用，也可手动调用。
    """
    try:
        from db.session import get_engine

        engine = get_engine()
        if engine is None:
            return
        pool = engine.sync_engine.pool
        DB_POOL_CHECKED_OUT.set(pool.checkedout())
        DB_POOL_SIZE.set(pool.size() + pool.overflow())
    except Exception:
        pass


class _DBPoolCollector:
    """Prometheus 自定义 Collector，在每次 scrape 时触发 DB 连接池指标更新。"""

    def describe(self):
        return []

    def collect(self):
        update_db_pool_metrics()
        return []


prometheus_client.REGISTRY.register(_DBPoolCollector())


def setup_metrics(app):
    """初始化并挂载 Prometheus 指标"""
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_instrument_requests_inprogress=True,
        excluded_handlers=[".*admin.*", "/metrics", "/health", "/"],
        inprogress_name="webhook_requests_inprogress",
        inprogress_labels=True,
    )
    instrumentator.instrument(app)

    if Config.server.METRICS_PORT > 0 and Config.server.METRICS_PORT != Config.server.PORT:
        # 启动独立的 Prometheus metrics 服务器
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
        # 复用主程序的端口
        logger.info(f"[Metrics] 复用主程序端口暴露 Prometheus 监控: {Config.server.PORT}/metrics")
        instrumentator.expose(app, endpoint="/metrics")

    return instrumentator
