import os
import logging
from dotenv import load_dotenv

load_dotenv()

# 配置模块的 logger（避免循环导入）
_config_logger = logging.getLogger('config')


import os
import logging
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional, List, Dict, Any

load_dotenv()

_config_logger = logging.getLogger('config')

class _AppConfig(BaseSettings):
    """应用配置类"""
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    # 服务器配置
    PORT: int = Field(default=8000)
    HOST: str = Field(default='0.0.0.0')
    DEBUG: bool = Field(default=False) # Workaround for backward compatibility

    # 安全配置
    WEBHOOK_SECRET: str = Field(default='')

    # 日志配置
    LOG_LEVEL: str = Field(default='INFO')
    LOG_FILE: str = Field(default='logs/webhook.log')

    # 数据存储配置
    DATA_DIR: str = Field(default='webhooks_data')
    ENABLE_FILE_BACKUP: bool = Field(default=False)
    
    # Redis 配置
    REDIS_URL: str = Field(default='redis://localhost:6379/0')

    # 数据库配置
    DATABASE_URL: str = Field(default='postgresql://postgres:postgres@localhost:5432/webhooks')
    DB_POOL_SIZE: int = Field(default=5)
    DB_MAX_OVERFLOW: int = Field(default=10)
    DB_POOL_RECYCLE: int = Field(default=3600)
    DB_POOL_TIMEOUT: int = Field(default=30)

    # AI 分析和转发配置
    ENABLE_AI_ANALYSIS: bool = Field(default=True)
    ENABLE_AI_DEGRADATION: bool = Field(default=False)
    FORWARD_URL: str = Field(default='')
    ENABLE_FORWARD: bool = Field(default=True)

    # OpenAI API 配置
    OPENAI_API_KEY: str = Field(default='')
    OPENAI_API_URL: str = Field(default='https://openrouter.ai/api/v1')
    OPENAI_MODEL: str = Field(default='anthropic/claude-sonnet-4')
    OPENAI_TEMPERATURE: float = Field(default=0.2)
    OPENAI_MAX_TOKENS: int = Field(default=1800)
    OPENAI_TRUNCATION_RETRY_MAX_TOKENS: int = Field(default=2600)

    # AI 提示词配置
    AI_SYSTEM_PROMPT: str = Field(
        default='你是一个专业的 DevOps 和系统运维专家，擅长分析 webhook 事件并提供准确的运维建议。你的职责是：1. 快速识别事件类型和严重程度 2. 提供清晰的问题摘要 3. 给出可执行的处理建议 4. 识别潜在风险和影响范围 5. 建议监控和预防措施 重要：你必须始终返回严格符合 JSON 标准的格式，不要使用注释、尾随逗号或单引号。'
    )
    AI_USER_PROMPT_FILE: str = Field(default='prompts/webhook_analysis_detailed.txt')
    AI_USER_PROMPT: str = Field(default='')

    # 重复告警去重配置
    DUPLICATE_ALERT_TIME_WINDOW: int = Field(default=24)
    FORWARD_DUPLICATE_ALERTS: bool = Field(default=False)
    REANALYZE_AFTER_TIME_WINDOW: bool = Field(default=True)
    FORWARD_AFTER_TIME_WINDOW: bool = Field(default=True)

    # 周期性提醒配置
    ENABLE_PERIODIC_REMINDER: bool = Field(default=True)
    REMINDER_INTERVAL_HOURS: int = Field(default=6)

    # 并发与通知窗口配置（秒）
    PROCESSING_LOCK_TTL_SECONDS: int = Field(default=120)
    PROCESSING_LOCK_WAIT_SECONDS: int = Field(default=3)
    RECENT_BEYOND_WINDOW_REUSE_SECONDS: int = Field(default=30)
    NOTIFICATION_COOLDOWN_SECONDS: int = Field(default=60)

    # 保存重试配置
    SAVE_MAX_RETRIES: int = Field(default=3)
    SAVE_RETRY_DELAY_SECONDS: float = Field(default=0.1)

    # 告警智能降噪 + 根因分析配置
    ENABLE_ALERT_NOISE_REDUCTION: bool = Field(default=True)
    NOISE_REDUCTION_WINDOW_MINUTES: int = Field(default=5)
    ROOT_CAUSE_MIN_CONFIDENCE: float = Field(default=0.65)
    SUPPRESS_DERIVED_ALERT_FORWARD: bool = Field(default=True)

    # AIOps 升级配置
    CACHE_ENABLED: bool = Field(default=True)
    ANALYSIS_CACHE_TTL: int = Field(default=21600)
    SMART_ROUTING_ENABLED: bool = Field(default=True)
    AI_COST_PER_1K_INPUT_TOKENS: float = Field(default=0.003)
    AI_COST_PER_1K_OUTPUT_TOKENS: float = Field(default=0.015)

    # 飞书通知重要性配置
    IMPORTANCE_CONFIG: Dict[str, Any] = Field(default={
        'high': {'color': 'red', 'emoji': '🔴', 'text': '高'},
        'medium': {'color': 'orange', 'emoji': '🟠', 'text': '中'},
        'low': {'color': 'green', 'emoji': '🟢', 'text': '低'}
    })

    # ChatOps 配置
    CHATOPS_ENABLED: bool = Field(default=False)
    FEISHU_BOT_APP_ID: str = Field(default='')
    FEISHU_BOT_APP_SECRET: str = Field(default='')

    # OpenClaw 深度分析引擎
    OPENCLAW_ENABLED: bool = Field(default=False)
    OPENCLAW_GATEWAY_URL: str = Field(default='http://127.0.0.1:18900')
    OPENCLAW_GATEWAY_TOKEN: str = Field(default='')
    OPENCLAW_HOOKS_TOKEN: str = Field(default='')
    OPENCLAW_HTTP_API_URL: str = Field(default='http://127.0.0.1:8085')
    OPENCLAW_TIMEOUT_SECONDS: int = Field(default=300)
    DEEP_ANALYSIS_ENGINE: str = Field(default='local')
    DEEP_ANALYSIS_PLATFORM: str = Field(default='openclaw')  # openclaw | hermes

    OPENCLAW_STABILITY_REQUIRED_HITS: int = Field(default=2)
    OPENCLAW_MIN_WAIT_SECONDS: int = Field(default=30)
    OPENCLAW_MAX_CONSECUTIVE_ERRORS: int = Field(default=5)
    OPENCLAW_ENABLE_DEGRADATION: bool = Field(default=False)
    
    DEEP_ANALYSIS_FEISHU_WEBHOOK: str = Field(default='')

    OPENCLAW_CONNECT_TIMEOUT: int = Field(default=10)
    OPENCLAW_HANDSHAKE_TIMEOUT: int = Field(default=5)
    OPENCLAW_RECV_TIMEOUT: float = Field(default=1.0)
    OPENCLAW_NONCE_TIMEOUT: float = Field(default=2.0)
    OPENCLAW_POLL_TIMEOUT: int = Field(default=90)

    AI_API_TIMEOUT: int = Field(default=10)
    FEISHU_WEBHOOK_TIMEOUT: int = Field(default=10)
    FORWARD_TIMEOUT: int = Field(default=10)
    
    # OpenClaw Device
    OPENCLAW_DEVICE_ID: str = Field(default='')
    OPENCLAW_DEVICE_PRIVATE_KEY_PEM: str = Field(default='')
    OPENCLAW_DEVICE_TOKEN: str = Field(default='')

    # Circuit Breakers
    CIRCUIT_BREAKER_FEISHU_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FEISHU_TIMEOUT: float = Field(default=30.0)
    
    CIRCUIT_BREAKER_OPENCLAW_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_OPENCLAW_TIMEOUT: float = Field(default=30.0)

    CIRCUIT_BREAKER_FORWARD_THRESHOLD: int = Field(default=5)
    CIRCUIT_BREAKER_FORWARD_TIMEOUT: float = Field(default=30.0)

    JSON_SORT_KEYS: bool = Field(default=False)
    JSONIFY_PRETTYPRINT_REGULAR: bool = Field(default=True)

    def validate(self) -> List[str]:
        warnings = []
        if not self.WEBHOOK_SECRET:
            warnings.append("WEBHOOK_SECRET 未配置，签名验证将被禁用")
        if self.ENABLE_AI_ANALYSIS and not self.OPENAI_API_KEY:
            warnings.append("ENABLE_AI_ANALYSIS=True 但 OPENAI_API_KEY 未配置，AI 分析将失败")
        if self.ENABLE_FORWARD and not self.FORWARD_URL:
            warnings.append("ENABLE_FORWARD=True 但 FORWARD_URL 未配置")
        for warning in warnings:
            _config_logger.warning(warning)
        return warnings

Config = _AppConfig()
