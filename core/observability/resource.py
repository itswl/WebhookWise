"""Resource metadata for OpenTelemetry providers.

All getters are cached: the values come from process-start-immutable
environment variables, and JsonFormatter reads most of them on every log
record, which would otherwise re-parse the environment per line.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from core.observability.attributes import (
    DEPLOYMENT_ENVIRONMENT,
    OTEL_SEMCONV_VERSION_DEFAULT,
    SERVICE_INSTANCE_ID,
    SERVICE_NAME,
    SERVICE_NAMESPACE,
    SERVICE_VERSION,
)
from core.version import __version__
from core.worker_identity import default_worker_id


@lru_cache(maxsize=8)
def get_service_name(default: str = "webhookwise") -> str:
    return os.getenv("OTEL_SERVICE_NAME", default).strip() or default


@lru_cache(maxsize=8)
def get_service_namespace(default: str = "webhookwise") -> str:
    return os.getenv("OTEL_SERVICE_NAMESPACE", default).strip() or default


@lru_cache(maxsize=1)
def get_service_version() -> str:
    return (
        os.getenv("OTEL_SERVICE_VERSION", "").strip()
        or os.getenv("SERVICE_VERSION", "").strip()
        or os.getenv("APP_VERSION", "").strip()
        or __version__
    )


@lru_cache(maxsize=1)
def get_deployment_environment() -> str:
    return os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "").strip() or os.getenv("APP_ENV", "production").strip()


@lru_cache(maxsize=1)
def get_service_instance_id() -> str:
    return (
        os.getenv("OTEL_SERVICE_INSTANCE_ID", "").strip()
        or os.getenv("SERVICE_INSTANCE_ID", "").strip()
        or default_worker_id()
    )


@lru_cache(maxsize=1)
def get_otel_semconv_version() -> str:
    return os.getenv("OTEL_SEMCONV_VERSION", OTEL_SEMCONV_VERSION_DEFAULT).strip() or OTEL_SEMCONV_VERSION_DEFAULT


@lru_cache(maxsize=1)
def get_otel_schema_url() -> str:
    configured = os.getenv("OTEL_SCHEMA_URL", "").strip()
    if configured:
        return configured
    return f"https://opentelemetry.io/schemas/{get_otel_semconv_version()}"


def _reset_resource_cache_for_tests() -> None:
    for cached in (
        get_service_name,
        get_service_namespace,
        get_service_version,
        get_deployment_environment,
        get_service_instance_id,
        get_otel_semconv_version,
        get_otel_schema_url,
    ):
        cached.cache_clear()


def build_resource(service_name: str | None = None) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            SERVICE_NAME: service_name or get_service_name(),
            SERVICE_NAMESPACE: get_service_namespace(),
            SERVICE_VERSION: get_service_version(),
            DEPLOYMENT_ENVIRONMENT: get_deployment_environment(),
            SERVICE_INSTANCE_ID: get_service_instance_id(),
        },
        schema_url=get_otel_schema_url(),
    )
