from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# 1. 业务吞吐与状态指标
WEBHOOK_RECEIVED_TOTAL = Counter(
    'webhook_received_total', 
    'Total number of webhooks received',
    ['source', 'status']
)

# 2. 智能降噪指标 (AIOps 核心价值)
WEBHOOK_NOISE_REDUCED_TOTAL = Counter(
    'webhook_noise_reduced_total',
    'Number of webhooks processed by noise reduction engine',
    ['source', 'relation', 'suppressed']
)

# 3. AI 成本与用量指标
AI_TOKENS_TOTAL = Counter(
    'ai_tokens_total',
    'Total number of tokens consumed by AI analysis',
    ['model', 'token_type'] # token_type: input, output
)

AI_COST_USD_TOTAL = Counter(
    'ai_cost_usd_total',
    'Total estimated cost of AI analysis in USD',
    ['model']
)

# 4. 性能指标
AI_ANALYSIS_DURATION_SECONDS = Histogram(
    'ai_analysis_duration_seconds',
    'Time spent on AI analysis',
    ['source', 'engine'],
    buckets=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, float("inf"))
)

# 5. 系统状态指标
DATABASE_EVENTS_COUNT = Gauge(
    'database_webhook_events_count',
    'Current number of webhook events in active table'
)

import prometheus_client

from core.config import Config
from core.logger import logger


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
    
    if Config.METRICS_PORT > 0 and Config.METRICS_PORT != Config.PORT:
        # 启动独立的 Prometheus metrics 服务器
        try:
            prometheus_client.start_http_server(port=Config.METRICS_PORT, addr=Config.HOST)
            logger.info(f"[Metrics] 成功启动独立的 Prometheus 监控端口: {Config.METRICS_PORT}")
        except OSError as e: # noqa: PERF203
            if "Address already in use" in str(e):
                logger.debug(f"[Metrics] 独立监控端口 {Config.METRICS_PORT} 已被其他 Worker 绑定，复用该指标服务。")
            else:
                logger.error(f"[Metrics] 启动独立监控端口失败: {e}")
        # 注意：无论是否绑定成功，都不向主 FastAPI 挂载 /metrics 路由，实现端口隔离。
    else:
        # 复用主程序的端口
        logger.info(f"[Metrics] 复用主程序端口暴露 Prometheus 监控: {Config.PORT}/metrics")
        instrumentator.expose(app, endpoint="/metrics")
        
    return instrumentator
