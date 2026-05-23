"""Application resource context."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from fastapi import Request

from core.config import UnifiedConfigManager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from core.redis_client import RedisClient


@dataclass(slots=True)
class AppContext:
    config: UnifiedConfigManager = field(default_factory=UnifiedConfigManager)
    http_client: httpx.AsyncClient | None = None
    redis_client: RedisClient | None = None
    db_engine: AsyncEngine | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None

    async def ensure_http_client(self) -> httpx.AsyncClient:
        if self.http_client is None or self.http_client.is_closed:
            from core.http_client import build_http_client

            self.http_client = build_http_client(self.config)
        return self.http_client

    def ensure_redis_client(self) -> RedisClient:
        if self.redis_client is None:
            from core.redis_client import build_redis_client

            self.redis_client = build_redis_client(self.config)
        return self.redis_client

    async def ensure_db(self) -> async_sessionmaker[AsyncSession]:
        if self.session_factory is None or self.db_engine is None:
            from db.engine import build_engine_and_session_factory

            self.db_engine, self.session_factory = build_engine_and_session_factory(self.config)
        return self.session_factory

    async def close(
        self,
        *,
        close_db: bool = True,
        close_redis: bool = True,
        close_http: bool = True,
    ) -> None:
        if close_db and self.db_engine is not None:
            await self.db_engine.dispose()
            self.db_engine = None
            self.session_factory = None

        if close_redis and self.redis_client is not None:
            from core.redis_client import close_redis_client

            await close_redis_client(self.redis_client)
            self.redis_client = None

        if close_http and self.http_client is not None and not self.http_client.is_closed:
            await self.http_client.aclose()
            self.http_client = None


_default_context: ContextVar[AppContext | None] = ContextVar("default_app_context", default=None)


def set_default_app_context(context: AppContext | None) -> None:
    _default_context.set(context)


def get_default_app_context() -> AppContext | None:
    return _default_context.get()


def init_default_app_context(config: UnifiedConfigManager | None = None) -> AppContext:
    context = AppContext(config=config or UnifiedConfigManager())
    _default_context.set(context)
    return context


def get_or_create_default_app_context(config: UnifiedConfigManager | None = None) -> AppContext:
    context = _default_context.get()
    if context is None:
        return init_default_app_context(config)
    if config is not None and context.config is not config:
        return init_default_app_context(config)
    return context


def get_config_manager() -> UnifiedConfigManager:
    context = get_default_app_context()
    return context.config if context is not None else UnifiedConfigManager()


def get_http_client_dependency(request: Request) -> httpx.AsyncClient:
    context = getattr(request.app.state, "app_context", None)
    if isinstance(context, AppContext):
        client = context.http_client
        if isinstance(client, httpx.AsyncClient) and not client.is_closed:
            return client
    from core.http_client import get_http_client

    return get_http_client()
