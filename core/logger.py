import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from urllib.parse import urlparse

from pythonjsonlogger import jsonlogger

from core.config import Config
from core.log_context import get_log_context
from core.trace import get_trace_id


def mask_url(url: str) -> str:
    """安全脱敏 URL，移除用户名和密码。"""
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://***@{parsed.hostname}{port}{parsed.path}"
        return "***"
    except Exception:
        return "***"


class TraceIdFilter(logging.Filter):
    """为每条日志记录注入当前协程的 trace_id。"""

    def filter(self, record):
        record.trace_id = get_trace_id() or "-"

        ctx = get_log_context()
        if ctx:
            for k, v in ctx.items():
                setattr(record, k, v)

        return True


def setup_logger():
    """初始化全局日志系统"""
    log_level = getattr(logging, Config.server.LOG_LEVEL.upper(), logging.INFO)
    logger = logging.getLogger("webhook_service")
    logger.setLevel(log_level)

    if logger.handlers:
        return logger

    # 1. JSON 格式化器
    # 强制包含基础字段，其他字段通过 extra 传入
    format_str = "%(timestamp)s %(level)s %(name)s %(message)s %(trace_id)s"
    formatter = jsonlogger.JsonFormatter(format_str)

    # 2. 处理器：控制台 + 滚动文件
    handlers = []

    # 控制台
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(formatter)
    handlers.append(stdout_h)

    # 文件
    log_file = Config.server.LOG_FILE
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_h = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_h.setFormatter(formatter)
        handlers.append(file_h)

    # 3. 异步处理：使用 QueueListener 避免日志 I/O 阻塞主线程
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    logger.addHandler(queue_handler)

    # 注册过滤器
    queue_handler.addFilter(TraceIdFilter())

    # 启动后台监听线程
    global _log_listener
    _log_listener = QueueListener(log_queue, *handlers, respect_handler_level=True)
    _log_listener.start()

    return logger


_log_listener = None


def stop_log_listener():
    """停止日志后台线程（供应用关闭时调用）"""
    global _log_listener
    if _log_listener:
        _log_listener.stop()
        _log_listener = None


def get_logger(name: str):
    """获取子模块 logger，继承主 logger 配置"""
    if name == "webhook_service":
        return setup_logger()

    # 创建子 logger
    child_logger = logging.getLogger(f"webhook_service.{name}")
    return child_logger


# 创建全局 logger 实例
logger = setup_logger()
