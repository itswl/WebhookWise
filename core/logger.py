import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from urllib.parse import urlparse

from pythonjsonlogger import jsonlogger

from core.config import Config, policies
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
    except Exception: return "***"


class TraceIdFilter(logging.Filter):
    """为每条日志记录注入当前协程的 trace_id。"""

    def filter(self, record):
        record.trace_id = get_trace_id() or "-"
        ctx = get_log_context()
        record.event_id = ctx.get("event_id") or "-"
        record.alert_hash = ctx.get("alert_hash") or "-"
        record.source = ctx.get("source") or "-"
        record.processing_status = ctx.get("processing_status") or "-"
        record.route_type = ctx.get("route_type") or "-"
        return True


# 全局 QueueListener 引用，供 shutdown 时调用 stop()
_log_listener: QueueListener | None = None


def setup_logger():
    """设置日志记录器（支持日志轮转和结构化日志）

    使用 QueueHandler + QueueListener 避免同步磁盘 I/O 阻塞事件循环：
    - 主 logger 通过 QueueHandler 将日志记录非阻塞地写入内存队列
    - QueueListener 在后台线程消费队列，将日志写入实际 handler
    """
    global _log_listener

    # 创建日志目录
    log_dir = os.path.dirname(Config.server.LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 解析日志级别
    log_level = getattr(logging, policies.server.LOG_LEVEL.upper(), logging.INFO)

    # 创建 logger
    logger = logging.getLogger("webhook_service")
    logger.setLevel(log_level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 添加 TraceIdFilter 到 logger（直接日志）
    trace_filter = TraceIdFilter()
    logger.addFilter(trace_filter)

    # 标准日志格式（控制台，含 trace_id）
    console_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(trace_id)s] [%(event_id)s] [%(source)s] [%(processing_status)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件处理器（支持轮转，最大 10MB，保留 5 个备份）
    file_handler = RotatingFileHandler(
        Config.server.LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)

    # 文件使用结构化 JSON 日志
    json_formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(trace_id)s %(event_id)s %(alert_hash)s %(source)s %(processing_status)s %(route_type)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(json_formatter)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)

    # 使用 QueueHandler + QueueListener 实现异步日志写入
    log_queue: queue.Queue = queue.Queue(-1)  # 无限队列
    queue_handler = QueueHandler(log_queue)
    queue_handler.setLevel(log_level)
    queue_handler.addFilter(trace_filter)  # 确保子 logger propagation 也注入 trace_id

    # QueueListener 在后台线程消费队列，将日志写入 file_handler 和 console_handler
    _log_listener = QueueListener(log_queue, file_handler, console_handler, respect_handler_level=True)
    _log_listener.start()

    # 主 logger 仅挂载 QueueHandler（非阻塞）
    logger.addHandler(queue_handler)

    # 设置第三方库的日志级别，防止干扰
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    return logger


def stop_log_listener() -> None:
    """停止日志队列监听器，确保所有缓冲日志刷写到磁盘。

    应在应用 shutdown 时调用。
    """
    global _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        _log_listener = None


def get_logger(name: str = "webhook_service") -> logging.Logger:
    """获取指定名称的 logger，继承主 logger 配置"""
    if name == "webhook_service":
        return setup_logger()

    # 创建子 logger
    child_logger = logging.getLogger(f"webhook_service.{name}")
    return child_logger


# 创建全局 logger 实例
logger = setup_logger()
