import logging
import os
from datetime import datetime
from config import Config


def setup_logger():
    """设置日志记录器"""
    
    # 创建日志目录
    log_dir = os.path.dirname(Config.LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # 创建 logger
    logger = logging.getLogger('webhook_service')
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 文件处理器
    file_handler = logging.FileHandler(Config.LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if Config.DEBUG else logging.INFO)
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


# 创建全局 logger 实例
logger = setup_logger()
