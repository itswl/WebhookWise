"""Application resource context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from core.config import Config, UnifiedConfigManager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from core.redis_client import RedisClient


@dataclass(slots=True)
class AppContext:
    config: UnifiedConfigManager = Config
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
            from db.session import build_engine_and_session_factory

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


_default_context: AppContext | None = None


def set_default_app_context(context: AppContext | None) -> None:
    global _default_context
    _default_context = context


def get_default_app_context() -> AppContext | None:
    return _default_context


def get_or_create_default_app_context(config: UnifiedConfigManager = Config) -> AppContext:
    global _default_context
    if _default_context is None or _default_context.config is not config:
        _default_context = AppContext(config=config)
    return _default_context
