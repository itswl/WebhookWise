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
    LOG_FILE = 'logs/webhook.log'
    
    # 数据存储配置
    DATA_DIR = 'webhooks_data'
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
    FORWARD_URL = os.getenv('FORWARD_URL', 'http://92.38.131.57:8000/webhook')
    ENABLE_FORWARD = os.getenv('ENABLE_FORWARD', 'true').lower() == 'true'
    
    # OpenAI API 配置
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_API_URL = os.getenv('OPENAI_API_URL', 'https://openrouter.ai/api/v1')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'anthropic/claude-sonnet-4')
    
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
    
    # JSON 配置
    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = True
    
    # 飞书通知重要性配置
    IMPORTANCE_CONFIG = {
        'high': {'color': 'red', 'emoji': '🔴', 'text': '高'},
        'medium': {'color': 'orange', 'emoji': '🟠', 'text': '中'},
        'low': {'color': 'green', 'emoji': '🟢', 'text': '低'}
    }
    
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
