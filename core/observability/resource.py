"""Resource metadata for OpenTelemetry providers."""

from __future__ import annotations

import os
import socket
from typing import Any

from core.observability.attributes import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_INSTANCE_ID,
    SERVICE_NAME,
    SERVICE_VERSION,
)


def get_service_name(default: str = "webhookwise") -> str:
    return os.getenv("OTEL_SERVICE_NAME", default).strip() or default


def get_service_version() -> str:
    return (
        os.getenv("OTEL_SERVICE_VERSION", "").strip()
        or os.getenv("SERVICE_VERSION", "").strip()
        or os.getenv("APP_VERSION", "").strip()
        or "unknown"
    )


def get_deployment_environment() -> str:
    return os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "").strip() or os.getenv("APP_ENV", "production").strip()


def get_service_instance_id() -> str:
    return (
        os.getenv("OTEL_SERVICE_INSTANCE_ID", "").strip()
        or os.getenv("SERVICE_INSTANCE_ID", "").strip()
        or f"{socket.gethostname()}-{os.getpid()}"
    )


def build_resource(service_name: str | None = None) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            SERVICE_NAME: service_name or get_service_name(),
            SERVICE_VERSION: get_service_version(),
            DEPLOYMENT_ENVIRONMENT: get_deployment_environment(),
            SERVICE_INSTANCE_ID: get_service_instance_id(),
        }
    )
