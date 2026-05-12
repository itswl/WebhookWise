"""FastAPI dependency providers for application-wide resources."""

from __future__ import annotations

import httpx
from fastapi import Request

from core.config import Config, UnifiedConfigManager
from core.http_client import get_http_client


def get_config_manager() -> UnifiedConfigManager:
    """Return the runtime-aware configuration manager.

    FastAPI tests can override this dependency without monkeypatching the
    process-global Config object.
    """
    return Config


def get_http_client_dependency(request: Request) -> httpx.AsyncClient:
    """Return the app-managed HTTP client when available."""
    client = getattr(request.app.state, "http_client", None)
    if isinstance(client, httpx.AsyncClient) and not client.is_closed:
        return client
    return get_http_client()
