import os
from collections.abc import Callable
from typing import cast

import prometheus_client
from fastapi import FastAPI
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

ALERT_NUMERIC_PARSE_FAILURE_TOTAL = Counter(
    "alert_numeric_parse_failure_total",
    "Alert numeric field parse failures during rule analysis",
    ["source", "field", "reason"],
)

# 9. Scheduler 轮询健康指标
SCHEDULED_TASK_RUNS_TOTAL = Counter(
    "scheduled_task_runs_total",
    "TaskIQ 定时任务执行次数",
    ["name", "status"],  # status: success, error
)

SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME = Gauge(
    "scheduled_task_last_success_unixtime",
    "TaskIQ 定时任务最近一次成功执行时间（Unix time）",
    ["name"],
    multiprocess_mode="livemax",
)

SCHEDULED_TASK_LAG_SECONDS = Gauge(
    "scheduled_task_lag_seconds",
    "TaskIQ 定时任务相对期望周期的滞后秒数（越大表示可能积压/漏跑）",
    ["name"],
    multiprocess_mode="livemax",
)

SCHEDULED_TASK_DURATION_SECONDS = Histogram(
    "scheduled_task_duration_seconds",
    "TaskIQ 定时任务单次执行耗时（秒）",
    ["name"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, float("inf")),
)

# 7. 转发重试指标
FORWARD_RETRY_TOTAL = Counter(
    "forward_retry_total",
    "转发重试结果计数",
    ["status"],  # status: success, exhausted, failed
)

# 8. 深度分析结果指标
DEEP_ANALYSIS_TOTAL = Counter(
    "deep_analysis_total",
    "深度分析任务结果计数",
    ["status", "engine"],  # status: completed, failed, timeout, degraded
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

_background_metrics_started = False


def update_db_pool_metrics() -> None:
    """更新数据库连接池容量指标。

    checked_out 已通过 Pool 事件回调实时更新（见 db/session.py），
    此函数仅更新连接池容量上限（极少变化）。
    """
    try:
        from db.session import get_db_pool_capacity, get_engine

        engine = get_engine()
        if engine is None:
            return
        cap = get_db_pool_capacity(engine)
        if cap is not None:
            DB_POOL_SIZE.set(cap)
    except (AttributeError, RuntimeError) as e:
        logger.warning("[Metrics] 无法获取 DB 连接池容量: %s", e)


def start_background_metrics_server() -> None:
    """Expose metrics for non-HTTP processes such as TaskIQ workers."""
    global _background_metrics_started
    if _background_metrics_started or Config.server.METRICS_PORT <= 0:
        return
    bind_host = Config.server.HOST or "127.0.0.1"
    try:
        multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
        if multiproc_dir:
            from prometheus_client import CollectorRegistry, multiprocess

            registry = CollectorRegistry()
            mp_collector = cast(Callable[[CollectorRegistry], object], multiprocess.MultiProcessCollector)
            mp_collector(registry)
            prometheus_client.start_http_server(port=Config.server.METRICS_PORT, addr=bind_host, registry=registry)
        else:
            prometheus_client.start_http_server(port=Config.server.METRICS_PORT, addr=bind_host)
        _background_metrics_started = True
        logger.info("[Metrics] 后台进程指标端口已启动: %s:%d", bind_host, Config.server.METRICS_PORT)
    except OSError as e:
        if "Address already in use" in str(e):
            logger.debug("[Metrics] 后台指标端口 %d 已被绑定", Config.server.METRICS_PORT)
            _background_metrics_started = True
            return
        logger.error("[Metrics] 后台指标端口启动失败: %s", e)


def setup_metrics(app: FastAPI) -> Instrumentator:
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
    update_db_pool_metrics()

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

        async def metrics_endpoint(request: object) -> Response:
            registry = CollectorRegistry()
            mp_collector = cast(Callable[[CollectorRegistry], object], multiprocess.MultiProcessCollector)
            mp_collector(registry)
            data = generate_latest(registry)
            return Response(content=data, media_type=CONTENT_TYPE_LATEST)

        app.add_route("/metrics", metrics_endpoint, methods=["GET"])
    elif Config.server.METRICS_PORT > 0 and Config.server.METRICS_PORT != Config.server.PORT:
        # ── 单进程 + 独立端口模式 ──
        start_background_metrics_server()
        # 注意：无论是否绑定成功，都不向主 FastAPI 挂载 /metrics 路由，实现端口隔离。
    else:
        # ── 单进程 + 复用主端口模式 ──
        logger.info("[Metrics] 复用主程序端口暴露 Prometheus 监控: %d/metrics", Config.server.PORT)
        instrumentator.expose(app, endpoint="/metrics")

    return instrumentator
