"""FastAPI dependency providers."""

from __future__ import annotations

import httpx
from fastapi import Request

from core.app_context import AppContext, get_or_create_default_app_context
from core.config import Config, UnifiedConfigManager
from core.http_client import get_http_client


def get_config_manager() -> UnifiedConfigManager:
    """Return the runtime-aware configuration manager from AppContext."""
    return get_or_create_default_app_context(Config).config


def get_http_client_dependency(request: Request) -> httpx.AsyncClient:
    """Return the app-owned HTTP client for request handlers."""
    context = getattr(request.app.state, "app_context", None)
    if isinstance(context, AppContext):
        client = context.http_client
        if isinstance(client, httpx.AsyncClient) and not client.is_closed:
            return client
    return get_http_client()
