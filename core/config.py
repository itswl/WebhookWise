import os
import logging
from dotenv import load_dotenv

load_dotenv()

# 配置模块的 logger（避免循环导入）
_config_logger = logging.getLogger('config')


class Config:
    """应用配置类"""
    
    # 服务器配置
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    DEBUG = os.getenv('FLASK_ENV', 'development') == 'development'
    
    # 安全配置（必须通过环境变量配置）
    WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
    
    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', 'logs/webhook.log')

    # 数据存储配置
    DATA_DIR = os.getenv('DATA_DIR', 'webhooks_data')
    ENABLE_FILE_BACKUP = os.getenv('ENABLE_FILE_BACKUP', 'false').lower() == 'true'  # 是否启用文件备份
    
    # 数据库配置
    DATABASE_URL = os.getenv(
        'DATABASE_URL',
        'postgresql://postgres:postgres@localhost:5432/webhooks'
    )
    
    # 数据库连接池配置
    DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '5'))  # 连接池大小
    DB_MAX_OVERFLOW = int(os.getenv('DB_MAX_OVERFLOW', '10'))  # 最大溢出连接数
    DB_POOL_RECYCLE = int(os.getenv('DB_POOL_RECYCLE', '3600'))  # 连接回收时间(秒)
    DB_POOL_TIMEOUT = int(os.getenv('DB_POOL_TIMEOUT', '30'))  # 连接超时(秒)
    
    # AI 分析和转发配置
    ENABLE_AI_ANALYSIS = os.getenv('ENABLE_AI_ANALYSIS', 'true').lower() == 'true'
    FORWARD_URL = os.getenv('FORWARD_URL', '')
    ENABLE_FORWARD = os.getenv('ENABLE_FORWARD', 'true').lower() == 'true'
    
    # OpenAI API 配置
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_API_URL = os.getenv('OPENAI_API_URL', 'https://openrouter.ai/api/v1')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'anthropic/claude-sonnet-4')
    OPENAI_TEMPERATURE = float(os.getenv('OPENAI_TEMPERATURE', '0.2'))
    OPENAI_MAX_TOKENS = int(os.getenv('OPENAI_MAX_TOKENS', '1800'))
    OPENAI_TRUNCATION_RETRY_MAX_TOKENS = int(os.getenv('OPENAI_TRUNCATION_RETRY_MAX_TOKENS', '2600'))
    
    # AI 提示词配置
    AI_SYSTEM_PROMPT = os.getenv(
        'AI_SYSTEM_PROMPT',
        '你是一个专业的 DevOps 和系统运维专家，擅长分析 webhook 事件并提供准确的运维建议。'
        '你的职责是：'
        '1. 快速识别事件类型和严重程度 '
        '2. 提供清晰的问题摘要 '
        '3. 给出可执行的处理建议 '
        '4. 识别潜在风险和影响范围 '
        '5. 建议监控和预防措施 '
        '重要：你必须始终返回严格符合 JSON 标准的格式，不要使用注释、尾随逗号或单引号。'
    )

    # AI User Prompt 配置（支持文件路径或直接内容）
    AI_USER_PROMPT_FILE = os.getenv('AI_USER_PROMPT_FILE', 'prompts/webhook_analysis_detailed.txt')
    AI_USER_PROMPT = os.getenv('AI_USER_PROMPT', '')  # 如果设置了此环境变量，优先使用，不读取文件
    
    # 重复告警去重配置
    DUPLICATE_ALERT_TIME_WINDOW = int(os.getenv('DUPLICATE_ALERT_TIME_WINDOW', '24'))  # 小时
    FORWARD_DUPLICATE_ALERTS = os.getenv('FORWARD_DUPLICATE_ALERTS', 'false').lower() == 'true'  # 是否转发重复告警（窗口内）

    # 超过时间窗口后的行为配置
    REANALYZE_AFTER_TIME_WINDOW = os.getenv('REANALYZE_AFTER_TIME_WINDOW', 'true').lower() == 'true'  # 超过时间窗口后是否重新分析
    FORWARD_AFTER_TIME_WINDOW = os.getenv('FORWARD_AFTER_TIME_WINDOW', 'true').lower() == 'true'  # 超过时间窗口后是否推送（高风险告警）

    # 周期性提醒配置
    ENABLE_PERIODIC_REMINDER = os.getenv('ENABLE_PERIODIC_REMINDER', 'true').lower() == 'true'  # 是否启用周期性提醒
    REMINDER_INTERVAL_HOURS = int(os.getenv('REMINDER_INTERVAL_HOURS', '6'))  # 提醒间隔（小时），默认6小时

    # 并发与通知窗口配置（秒）
    PROCESSING_LOCK_TTL_SECONDS = int(os.getenv('PROCESSING_LOCK_TTL_SECONDS', '120'))
    PROCESSING_LOCK_WAIT_SECONDS = int(os.getenv('PROCESSING_LOCK_WAIT_SECONDS', '3'))
    RECENT_BEYOND_WINDOW_REUSE_SECONDS = int(os.getenv('RECENT_BEYOND_WINDOW_REUSE_SECONDS', '30'))
    NOTIFICATION_COOLDOWN_SECONDS = int(os.getenv('NOTIFICATION_COOLDOWN_SECONDS', '60'))

    # 保存重试配置
    SAVE_MAX_RETRIES = int(os.getenv('SAVE_MAX_RETRIES', '3'))
    SAVE_RETRY_DELAY_SECONDS = float(os.getenv('SAVE_RETRY_DELAY_SECONDS', '0.1'))

    # 告警智能降噪 + 根因分析配置
    ENABLE_ALERT_NOISE_REDUCTION = os.getenv('ENABLE_ALERT_NOISE_REDUCTION', 'true').lower() == 'true'
    NOISE_REDUCTION_WINDOW_MINUTES = int(os.getenv('NOISE_REDUCTION_WINDOW_MINUTES', '5'))
    ROOT_CAUSE_MIN_CONFIDENCE = float(os.getenv('ROOT_CAUSE_MIN_CONFIDENCE', '0.65'))
    SUPPRESS_DERIVED_ALERT_FORWARD = os.getenv('SUPPRESS_DERIVED_ALERT_FORWARD', 'true').lower() == 'true'
    
    # AI 分析结果缓存配置
    CACHE_ENABLED = os.getenv('CACHE_ENABLED', 'true').lower() == 'true'
    ANALYSIS_CACHE_TTL = int(os.getenv('ANALYSIS_CACHE_TTL', '21600'))  # 默认 6 小时（秒）
    
    # 智能分析路由配置
    SMART_ROUTING_ENABLED = os.getenv('SMART_ROUTING_ENABLED', 'true').lower() == 'true'
    
    # AI 成本追踪配置（估算价格，美元/1K tokens）
    AI_COST_PER_1K_INPUT_TOKENS = float(os.getenv('AI_COST_PER_1K_INPUT_TOKENS', '0.003'))  # $3/1M input
    AI_COST_PER_1K_OUTPUT_TOKENS = float(os.getenv('AI_COST_PER_1K_OUTPUT_TOKENS', '0.015'))  # $15/1M output
    
    # JSON 配置
    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = True
    
    # 飞书通知重要性配置
    IMPORTANCE_CONFIG = {
        'high': {'color': 'red', 'emoji': '🔴', 'text': '高'},
        'medium': {'color': 'orange', 'emoji': '🟠', 'text': '中'},
        'low': {'color': 'green', 'emoji': '🟢', 'text': '低'}
    }
    
    # ChatOps 配置
    CHATOPS_ENABLED = os.getenv('CHATOPS_ENABLED', 'false').lower() == 'true'
    FEISHU_BOT_APP_ID = os.getenv('FEISHU_BOT_APP_ID', '')
    FEISHU_BOT_APP_SECRET = os.getenv('FEISHU_BOT_APP_SECRET', '')
    
    # OpenOcta 集成配置
    OPENOCTA_ENABLED = os.getenv('OPENOCTA_ENABLED', 'false').lower() == 'true'
    OPENOCTA_GATEWAY_URL = os.getenv('OPENOCTA_GATEWAY_URL', 'http://127.0.0.1:18900')
    OPENOCTA_GATEWAY_TOKEN = os.getenv('OPENOCTA_GATEWAY_TOKEN', '')
    OPENOCTA_HOOKS_TOKEN = os.getenv('OPENOCTA_HOOKS_TOKEN', '')
    OPENOCTA_TIMEOUT_SECONDS = int(os.getenv('OPENOCTA_TIMEOUT_SECONDS', '300'))
    DEEP_ANALYSIS_ENGINE = os.getenv('DEEP_ANALYSIS_ENGINE', 'local')  # local | openocta | auto

    # OpenOcta/OpenClaw 轮询稳定性参数
    OPENCLAW_STABILITY_REQUIRED_HITS = int(os.getenv('OPENCLAW_STABILITY_REQUIRED_HITS', '2'))   # 连续 N 次一致才确认完成
    OPENCLAW_MIN_WAIT_SECONDS = int(os.getenv('OPENCLAW_MIN_WAIT_SECONDS', '30'))              # 创建后最少等待秒数再开始轮询
    OPENCLAW_MAX_CONSECUTIVE_ERRORS = int(os.getenv('OPENCLAW_MAX_CONSECUTIVE_ERRORS', '5'))   # 连续超时最大次数
    
    # 深度分析飞书通知配置
    DEEP_ANALYSIS_FEISHU_WEBHOOK = os.getenv('DEEP_ANALYSIS_FEISHU_WEBHOOK', '')

    # OpenOcta WebSocket 连接超时配置（秒）
    OPENOCTA_CONNECT_TIMEOUT = int(os.getenv('OPENOCTA_CONNECT_TIMEOUT', '10'))    # TCP + WS 握手超时
    OPENOCTA_HANDSHAKE_TIMEOUT = int(os.getenv('OPENOCTA_HANDSHAKE_TIMEOUT', '5'))  # OpenOcta 协议握手超时
    OPENOCTA_RECV_TIMEOUT = float(os.getenv('OPENOCTA_RECV_TIMEOUT', '1.0'))        # recv 超时（检查 _done 事件）
    OPENOCTA_NONCE_TIMEOUT = float(os.getenv('OPENOCTA_NONCE_TIMEOUT', '2.0'))      # 接收 nonce challenge 超时
    OPENOCTA_POLL_TIMEOUT = int(os.getenv('OPENOCTA_POLL_TIMEOUT', '90'))           # 轮询结果超时（高延迟网络适配）

    # HTTP 请求超时配置（秒）
    AI_API_TIMEOUT = int(os.getenv('AI_API_TIMEOUT', '10'))              # AI API 请求超时
    FEISHU_WEBHOOK_TIMEOUT = int(os.getenv('FEISHU_WEBHOOK_TIMEOUT', '10'))  # 飞书 webhook 请求超时
    FORWARD_TIMEOUT = int(os.getenv('FORWARD_TIMEOUT', '10'))          # 转发请求超时
    
    @classmethod
    def validate(cls) -> list[str]:
        """
        验证必需配置，返回警告信息列表
        
        Returns:
            list[str]: 警告信息列表
        """
        warnings = []
        
        # 检查安全配置
        if not cls.WEBHOOK_SECRET:
            warnings.append("WEBHOOK_SECRET 未配置，签名验证将被禁用")
        
        # 检查 AI 分析配置
        if cls.ENABLE_AI_ANALYSIS and not cls.OPENAI_API_KEY:
            warnings.append("ENABLE_AI_ANALYSIS=True 但 OPENAI_API_KEY 未配置，AI 分析将失败")
        
        # 检查转发配置
        if cls.ENABLE_FORWARD and not cls.FORWARD_URL:
            warnings.append("ENABLE_FORWARD=True 但 FORWARD_URL 未配置")
        
        # 输出警告日志
        for warning in warnings:
            _config_logger.warning(warning)
        
        return warnings
