"""OTLP log export for standard Python logging."""

from __future__ import annotations

import logging
from typing import Any, cast

from core.observability.exporters import build_log_exporter, otel_enabled
from core.observability.resource import build_resource

_provider_initialized = False
_handler_installed = False


def setup_logging(*, service_name: str | None = None, logger_name: str = "webhook_service") -> None:
    global _handler_installed, _provider_initialized
    if not otel_enabled():
        return
    exporter = build_log_exporter()
    if exporter is None:
        logging.getLogger(logger_name).warning("[OTEL] logs enabled but no log exporter is configured")
        return

    try:
        from opentelemetry import _logs
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError:
        return

    provider: Any
    if not _provider_initialized:
        provider = LoggerProvider(resource=build_resource(service_name))
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        _logs.set_logger_provider(provider)
        _provider_initialized = True
    else:
        provider = _logs.get_logger_provider()

    if _handler_installed:
        return

    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    cast(Any, handler)._webhookwise_otel_handler = True
    try:
        from core.logger import TraceIdFilter

        handler.addFilter(TraceIdFilter())
    except ImportError:
        pass

    app_logger = logging.getLogger(logger_name)
    if not any(getattr(existing, "_webhookwise_otel_handler", False) for existing in app_logger.handlers):
        app_logger.addHandler(handler)
    _handler_installed = True
