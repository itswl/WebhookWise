import logging
import os
import queue
import sys
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener
from typing import Any
from urllib.parse import urlparse

from core import json
from core.config import AppConfig
from core.log_context import get_log_context
from core.logging_levels import apply_log_levels
from core.observability.attributes import normalize_attribute_key
from core.observability.resource import (
    get_deployment_environment,
    get_otel_schema_url,
    get_service_instance_id,
    get_service_name,
    get_service_namespace,
    get_service_version,
)
from core.observability.tracing import get_current_trace_id, get_otel_span_id, get_otel_trace_flags

_LEVEL_VALUE_BY_NAME = {
    "CRITICAL": "fatal",
    "ERROR": "error",
    "WARNING": "warn",
    "WARN": "warn",
    "INFO": "info",
    "DEBUG": "debug",
    "TRACE": "trace",
}
_DISPLAY_LEVEL_BY_VALUE = {
    "fatal": "FATAL",
    "error": "ERROR",
    "warn": "WARN",
    "info": "INFO",
    "debug": "DEBUG",
    "trace": "TRACE",
}


def normalize_log_level(level_name: str) -> str:
    return _LEVEL_VALUE_BY_NAME.get(str(level_name or "").upper(), str(level_name or "").lower() or "info")


def display_log_level(level_name: str) -> str:
    return _DISPLAY_LEVEL_BY_VALUE.get(normalize_log_level(level_name), str(level_name or "").upper())


def mask_url(url: str) -> str:
    """安全脱敏 URL，移除用户名、密码、query 以及可能包含 token 的 path 尾部。"""
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            path = parsed.path or ""
            safe_path = ""
            if path and path != "/":
                parts = [p for p in path.split("/") if p]
                safe_path = f"/{parts[0]}/..." if parts else ""
            return f"{parsed.scheme}://***@{parsed.hostname}{port}{safe_path}"
        return "***"
    except (AttributeError, ValueError):
        return "***"


class TraceIdFilter(logging.Filter):
    """Inject OTel correlation and canonical context attributes into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        otel_trace_id = ""
        otel_span_id = ""
        otel_trace_flags = "00"
        try:
            otel_trace_id = get_current_trace_id()
            otel_span_id = get_otel_span_id()
            otel_trace_flags = get_otel_trace_flags()
        except (RuntimeError, TypeError, ValueError):
            otel_trace_id = ""
            otel_span_id = ""
            otel_trace_flags = "00"

        record.trace_id = otel_trace_id or "-"
        record.span_id = otel_span_id or "-"
        record.trace_flags = otel_trace_flags
        record.severity = normalize_log_level(record.levelname)
        record.severity_text = display_log_level(record.levelname)
        setattr(record, "logger.name", record.name)

        ctx = get_log_context()
        if ctx:
            for k, v in ctx.items():
                attr_key = normalize_attribute_key(k)
                if attr_key:
                    setattr(record, attr_key, v)

        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = normalize_log_level(record.levelname)
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "observed_timestamp": timestamp,
            "severity_text": display_log_level(record.levelname),
            "severity_number": record.levelno,
            "severity": level,
            "body": record.getMessage(),
            "logger.name": record.name,
            "schema_url": get_otel_schema_url(),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            "trace_flags": getattr(record, "trace_flags", "00"),
            "service.name": get_service_name(),
            "service.namespace": get_service_namespace(),
            "service.version": get_service_version(),
            "deployment.environment": get_deployment_environment(),
            "service.instance.id": get_service_instance_id(),
        }

        reserved = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
        }

        if record.exc_info:
            exc_type, exc, _tb = record.exc_info
            payload["exception.type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exception.message"] = str(exc)
            payload["exception.stacktrace"] = self.formatException(record.exc_info)

        for k, v in record.__dict__.items():
            if k in reserved or k.startswith("_"):
                continue
            attr_key = normalize_attribute_key(k)
            if attr_key and attr_key not in payload:
                payload[attr_key] = v

        return json.dumps(payload)


def setup_logger(config: AppConfig | None = None) -> logging.Logger:
    """初始化全局日志系统"""
    global _log_listener, _logger_pid
    if config is None:
        from core.app_context import get_config_manager

        config = get_config_manager()
    apply_log_levels(config.server.LOG_LEVEL, config.server.THIRD_PARTY_LOG_LEVEL)
    logger = logging.getLogger("webhook_service")

    if logger.handlers:
        current_pid = os.getpid()
        if _logger_pid == current_pid:
            return logger
        # TaskIQ/worker 子进程会继承父进程的 QueueHandler，但 QueueListener
        # 线程不会跨 fork 存活。必须在当前 PID 重新安装 handler/listener，
        # 否则 webhook_service.* 业务日志会被写入无人消费的队列。
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        _log_listener = None

    logger.propagate = False

    # 1. JSON 格式化器
    # 强制包含基础字段，其他字段通过 extra 传入
    formatter: logging.Formatter = JsonFormatter()

    # 2. 处理器：stdout。OTLP logs are installed by core.observability.logging.
    handlers: list[logging.Handler] = []

    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(formatter)
    handlers.append(stdout_h)

    # 3. 异步处理：使用 QueueListener 避免日志 I/O 阻塞主线程
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    logger.addHandler(queue_handler)

    # 注册过滤器
    queue_handler.addFilter(TraceIdFilter())

    # 启动后台监听线程
    _log_listener = QueueListener(log_queue, *handlers, respect_handler_level=True)
    _log_listener.start()
    _logger_pid = os.getpid()

    return logger


_log_listener: QueueListener | None = None
_logger_pid: int | None = None
_root_logger: logging.Logger | None = None


def stop_log_listener() -> None:
    """停止日志后台线程（供应用关闭时调用）"""
    global _log_listener, _logger_pid
    if _log_listener:
        _log_listener.stop()
        _log_listener = None
        _logger_pid = None


def get_logger(name: str) -> logging.Logger:
    """获取子模块 logger，继承主 logger 配置"""
    global _root_logger
    _root_logger = setup_logger()
    if name == "webhook_service":
        return _root_logger

    # 创建子 logger
    child_logger = logging.getLogger(f"webhook_service.{name}")
    return child_logger


logger = logging.getLogger("webhook_service")
