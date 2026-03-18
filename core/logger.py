import logging
import os
from logging.handlers import RotatingFileHandler
from config import Config

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
    
    # 创建 logger
    logger = logging.getLogger('webhook_service')
    logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 标准日志格式（控制台）
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 文件处理器（支持轮转，最大 10MB，保留 5 个备份）
    file_handler = RotatingFileHandler(
        Config.LOG_FILE, 
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    
    # 文件使用结构化 JSON 日志（如果可用）
    if HAS_JSON_LOGGER:
        json_formatter = jsonlogger.JsonFormatter(
            '%(asctime)s %(name)s %(levelname)s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(json_formatter)
    else:
        file_handler.setFormatter(console_formatter)
    
    # 控制台处理器（保持可读格式）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if Config.DEBUG else logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


# 创建全局 logger 实例
logger = setup_logger()
