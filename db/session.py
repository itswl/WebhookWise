import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from core.logger import get_logger
from db.engine import build_engine_and_session_factory as build_engine_and_session_factory
from db.engine import dispose_engine as dispose_engine
from db.engine import get_db_pool_capacity as get_db_pool_capacity
from db.engine import get_db_pool_checked_out as get_db_pool_checked_out
from db.engine import get_engine as get_engine
from db.engine import init_engine as init_engine
from db.engine import test_db_connection as test_db_connection

_logger = get_logger("db.session")


class Base(DeclarativeBase):
    pass


def _app_context_from_request(request: "Request | None") -> object | None:
    from core.app_context import AppContext, get_default_app_context

    default_context = get_default_app_context()
    if request is not None:
        app = getattr(request, "app", None)
        state = getattr(app, "state", None)
        context = getattr(state, "app_context", None)
        if isinstance(context, AppContext):
            if context.session_factory is None and default_context is not None:
                return default_context
            return context

    return default_context


async def _ensure_session_factory(request: "Request | None" = None) -> async_sessionmaker[AsyncSession]:
    context = _app_context_from_request(request)
    if context is None:
        from core.app_context import get_or_create_default_app_context

        context = get_or_create_default_app_context()

    ensure_db = getattr(context, "ensure_db", None)
    if not callable(ensure_db):
        raise RuntimeError("AppContext is missing ensure_db()")
    return cast(async_sessionmaker[AsyncSession], await ensure_db())


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI Depends 异步生成器：只管理 session 生命周期。

    HTTP 写接口需要显式提交。这样路由可以在提交成功后再触发 TaskIQ
    或外部通知，避免依赖退出时才提交导致的事务/副作用顺序不清。
    """
    session_factory = await _ensure_session_factory(request)
    start = time.perf_counter()
    status = "success"
    try:
        from core.observability.tracing import span as otel_span

        with otel_span("db.session", {"db.operation": "request_session"}):
            async with session_factory() as session:
                yield session
    except Exception:
        status = "error"
        raise
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels("request_session", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("request_session", status).observe(time.perf_counter() - start)


@asynccontextmanager
async def session_scope(existing_session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """异步数据库事务上下文管理器。

    新建 session 时使用 SQLAlchemy 2.0 的 ``async_sessionmaker.begin()``，
    由框架负责提交、回滚和关闭。传入 existing_session 时不接管事务边界，
    由外层调用方负责提交或回滚。
    """
    start = time.perf_counter()
    operation = "existing_session" if existing_session else "transaction"
    status = "success"
    try:
        from core.observability.tracing import span as otel_span

        with otel_span("db.session", {"db.operation": operation}):
            if existing_session:
                yield existing_session
            else:
                session_factory = await _ensure_session_factory()
                async with session_factory.begin() as session:
                    yield session
    except Exception:
        status = "error"
        raise
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels(operation, status).inc()
        DB_SESSION_DURATION_SECONDS.labels(operation, status).observe(time.perf_counter() - start)


async def count_with_timeout(
    session: AsyncSession,
    stmt: Any,
    timeout_ms: int = 2000,
) -> int | None:
    """带 statement_timeout 兜底的 COUNT 查询（PostgreSQL-only）。

    超时返回 None，调用方应适配 None 场景。
    使用 SAVEPOINT 隔离超时查询，避免 rollback 摧毁调用者事务。
    """
    start = time.perf_counter()
    status = "success"
    try:
        async with session.begin_nested():
            with contextlib.suppress(Exception):
                await session.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))
            result = await session.execute(stmt)
            return result.scalar() or 0
    except Exception as e:
        status = "timeout_or_error"
        _logger.warning("COUNT query timeout (%dms): %s", timeout_ms, e)
        return None
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels("count_query", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("count_query", status).observe(time.perf_counter() - start)


__all__ = [
    "Base",
    "build_engine_and_session_factory",
    "count_with_timeout",
    "dispose_engine",
    "get_db_pool_capacity",
    "get_db_pool_checked_out",
    "get_db_session",
    "get_engine",
    "init_engine",
    "session_scope",
    "test_db_connection",
]
