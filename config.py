import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """应用配置类"""
    
    # 服务器配置
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    DEBUG = os.getenv('FLASK_ENV', 'development') == 'development'
    
    # 安全配置
    WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'default-secret-key')
    
    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = 'logs/webhook.log'
    
    # 数据存储配置
    DATA_DIR = 'webhooks_data'
    
    # 数据库配置
    DATABASE_URL = os.getenv(
        'DATABASE_URL',
        'postgresql://postgres:postgres@localhost:5432/webhooks'
    )
    
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
        '5. 建议监控和预防措施'
    )
    
    # 重复告警去重配置
    DUPLICATE_ALERT_TIME_WINDOW = int(os.getenv('DUPLICATE_ALERT_TIME_WINDOW', '24'))  # 小时
    FORWARD_DUPLICATE_ALERTS = os.getenv('FORWARD_DUPLICATE_ALERTS', 'false').lower() == 'true'  # 是否转发重复告警
    
    # JSON 配置
    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = True
