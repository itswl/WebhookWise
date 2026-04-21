import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from core.config import Config

# 尝试导入结构化日志库
try:
    from pythonjsonlogger import jsonlogger
    HAS_JSON_LOGGER = True
except ImportError:
    HAS_JSON_LOGGER = False


def setup_logger():
    """设置日志记录器（支持日志轮转和结构化日志）"""
    
    # 创建日志目录
    log_dir = os.path.dirname(Config.LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 解析日志级别
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    
    # 创建 logger
    logger = logging.getLogger('webhook_service')
    logger.setLevel(log_level)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 标准日志格式（控制台）
    console_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 文件处理器（支持轮转，最大 10MB，保留 5 个备份）
    file_handler = RotatingFileHandler(
        Config.LOG_FILE, 
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    
    # 文件使用结构化 JSON 日志（如果可用且配置允许）
    if HAS_JSON_LOGGER:
        json_formatter = jsonlogger.JsonFormatter(
            '%(asctime)s %(name)s %(levelname)s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(json_formatter)
    else:
        file_handler.setFormatter(console_formatter)
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # 设置第三方库的日志级别，防止干扰
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    
    return logger


def get_logger(name: str = 'webhook_service') -> logging.Logger:
    """获取指定名称的 logger，继承主 logger 配置"""
    if name == 'webhook_service':
        return setup_logger()
    
    # 创建子 logger
    child_logger = logging.getLogger(f'webhook_service.{name}')
    return child_logger


# 创建全局 logger 实例
logger = setup_logger()
