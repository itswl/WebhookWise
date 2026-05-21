import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import Config, UnifiedConfigManager
from core.logger import get_logger, mask_url

_logger = get_logger("db.session")


class Base(DeclarativeBase):
    pass


def _build_engine_kwargs(config: UnifiedConfigManager = Config) -> dict[str, Any]:
    """返回连接池公共参数"""
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": config.db.DB_POOL_SIZE,
        "max_overflow": config.db.DB_MAX_OVERFLOW,
        "pool_recycle": config.db.DB_POOL_RECYCLE,
        "pool_timeout": config.db.DB_POOL_TIMEOUT,
        "connect_args": {
            "server_settings": {
                "statement_timeout": str(config.db.DB_STATEMENT_TIMEOUT_MS),
                "synchronous_commit": config.db.DB_SYNC_COMMIT,
            }
        },
    }


def _async_url(config: UnifiedConfigManager = Config) -> str:
    """将 DATABASE_URL 的 driver 前缀安全替换为 asyncpg。

    不使用 make_url 解析，避免密码含 @#%: 等特殊字符时被误判为 URL 分隔符。
    """
    url = str(config.db.DATABASE_URL)
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+asyncpg://", 1)
    return url


# ────────────────────────────────────────
# 公共 API
# ────────────────────────────────────────


def build_engine_and_session_factory(
    config: UnifiedConfigManager = Config,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    _logger.info("[DB] 正在初始化异步数据库连接池: %s", mask_url(config.db.DATABASE_URL))
    engine = create_async_engine(_async_url(config), **_build_engine_kwargs(config))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        from core.observability.tracing import instrument_sqlalchemy

        instrument_sqlalchemy(engine.sync_engine)
    except Exception as e:
        _logger.debug("[DB] SQLAlchemy 自动探测失败（可能未安装 OTEL）: %s", e)
    _setup_pool_metrics(engine)
    return engine, session_factory


async def init_engine(config: UnifiedConfigManager = Config) -> None:
    """Ensure the current AppContext owns a DB engine and session factory."""
    from core.app_context import get_or_create_default_app_context

    context = get_or_create_default_app_context(config)
    await context.ensure_db()


def _setup_pool_metrics(engine: AsyncEngine) -> None:
    """Initialize DB pool gauges from the real SQLAlchemy pool state."""
    from core.observability.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE

    DB_POOL_SIZE.set_callback(lambda: get_db_pool_capacity(engine))
    DB_POOL_CHECKED_OUT.set_callback(lambda: get_db_pool_checked_out(engine))
    _logger.info("[DB] Pool metrics initialized from SQLAlchemy pool state")


def get_db_pool_capacity(engine: AsyncEngine) -> int | None:
    pool = engine.sync_engine.pool
    size = getattr(pool, "size", None)
    overflow = getattr(pool, "overflow", None)
    if not callable(size) or not callable(overflow):
        return None
    try:
        return int(size() + overflow())
    except Exception:
        return None


def get_db_pool_checked_out(engine: AsyncEngine) -> int | None:
    pool = engine.sync_engine.pool
    checkedout = getattr(pool, "checkedout", None)
    if not callable(checkedout):
        return None
    try:
        return int(checkedout())
    except Exception:
        return None


async def dispose_engine() -> None:
    """Close the DB engine owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.db_engine is not None:
        await context.close(close_redis=False, close_http=False)
        _logger.info("[DB] 当前上下文数据库引擎已关闭")


def get_engine() -> AsyncEngine | None:
    """Return the DB engine owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.db_engine is not None:
        return context.db_engine
    return None


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


async def test_db_connection() -> bool:
    """测试数据库连接（异步版本）"""
    start = time.perf_counter()
    status = "success"
    try:
        await init_engine()
        engine = get_engine()
        assert engine is not None
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        status = "error"
        _logger.error("数据库连接失败: %s", e)
        return False
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels("healthcheck", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("healthcheck", status).observe(time.perf_counter() - start)
