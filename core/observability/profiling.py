"""Optional continuous profiling integration."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from core.observability.exporters import env_int
from core.observability.events import emit_event
from core.observability.exporters import env_flag
from core.observability.resource import get_deployment_environment, get_service_name, get_service_version

_initialized = False
_TAG_KEY_RE = re.compile(r"[^a-zA-Z0-9_]")


def profiles_enabled() -> bool:
    return env_flag("PYROSCOPE_ENABLED", default=False) or env_flag("OTEL_PROFILES_ENABLED", default=False)


def _tag_key(key: str) -> str:
    sanitized = _TAG_KEY_RE.sub("_", key.strip())
    if not sanitized:
        return "unknown"
    if sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return sanitized


def _tags(service_name: str) -> dict[str, str]:
    tags = {
        "service_version": get_service_version(),
        "deployment_environment": get_deployment_environment(),
    }
    raw = os.getenv("PYROSCOPE_TAGS", "").strip()
    if not raw:
        return tags
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = _tag_key(key)
        value = value.strip()
        if key and value:
            tags[key] = value
    return tags


def _register_span_profiles() -> None:
    if not env_flag("PYROSCOPE_SPAN_PROFILES_ENABLED", default=True):
        return
    try:
        from opentelemetry import trace
        from pyroscope.otel import PyroscopeSpanProcessor
    except ImportError:
        return

    provider = trace.get_tracer_provider()
    add_span_processor = getattr(provider, "add_span_processor", None)
    if callable(add_span_processor):
        add_span_processor(PyroscopeSpanProcessor())


def setup_profiling(*, service_name: str | None = None) -> None:
    global _initialized
    if _initialized or not profiles_enabled():
        return

    app_name = os.getenv("PYROSCOPE_APPLICATION_NAME", "").strip() or service_name or get_service_name()
    server_address = os.getenv("PYROSCOPE_SERVER_ADDRESS", "").strip() or os.getenv("PYROSCOPE_URL", "").strip()
    logger = logging.getLogger("webhook_service")
    if not server_address:
        logger.warning("[Profiles] profiling enabled but PYROSCOPE_SERVER_ADDRESS is not configured")
        _initialized = True
        return

    try:
        import pyroscope
    except ImportError:
        logger.warning("[Profiles] profiling enabled but pyroscope-io is not installed")
        _initialized = True
        return

    kwargs: dict[str, Any] = {
        "application_name": app_name,
        "server_address": server_address,
        "sample_rate": env_int("PYROSCOPE_SAMPLE_RATE", 100),
        "oncpu": env_flag("PYROSCOPE_ONCPU", default=True),
        "gil_only": env_flag("PYROSCOPE_GIL_ONLY", default=True),
        "enable_logging": env_flag("PYROSCOPE_ENABLE_LOGGING", default=False),
        "tags": _tags(app_name),
    }
    if token := os.getenv("PYROSCOPE_AUTH_TOKEN", "").strip():
        kwargs["auth_token"] = token
    if username := os.getenv("PYROSCOPE_BASIC_AUTH_USERNAME", "").strip():
        kwargs["basic_auth_username"] = username
    if password := os.getenv("PYROSCOPE_BASIC_AUTH_PASSWORD", "").strip():
        kwargs["basic_auth_password"] = password
    if tenant_id := os.getenv("PYROSCOPE_TENANT_ID", "").strip():
        kwargs["tenant_id"] = tenant_id

    pyroscope.configure(**kwargs)
    _register_span_profiles()
    _initialized = True
    emit_event("profiles.started", {"profile.backend": "pyroscope", "profile.application": app_name})
