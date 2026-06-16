import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import AppConfig
from core.logger import get_logger, mask_url

_logger = get_logger("db.engine")


def _build_engine_kwargs(config: AppConfig) -> dict[str, Any]:
    """Return common connection pool parameters"""
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


def _async_url(config: AppConfig) -> str:
    """Safely replace the driver prefix of DATABASE_URL with asyncpg.

    Avoids using make_url for parsing, so that passwords containing special
    characters like @#%: are not misinterpreted as URL separators.
    """
    url = str(config.db.DATABASE_URL)
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+asyncpg://", 1)
    return url


def build_engine_and_session_factory(
    config: AppConfig | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    if config is None:
        from core.app_context import get_config_manager

        config = get_config_manager()
    _logger.info("[DB] Initializing async database connection pool: %s", mask_url(config.db.DATABASE_URL))
    engine = create_async_engine(_async_url(config), **_build_engine_kwargs(config))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        from core.observability.tracing import instrument_sqlalchemy

        instrument_sqlalchemy(engine.sync_engine)
    except Exception as e:
        _logger.debug("[DB] SQLAlchemy auto-instrumentation failed (OTEL may not be installed): %s", e)
    _setup_pool_metrics(engine)
    return engine, session_factory


async def init_engine(config: AppConfig | None = None) -> None:
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
        _logger.info("[DB] Database engine for the current context has been closed")


def get_engine() -> AsyncEngine | None:
    """Return the DB engine owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.db_engine is not None:
        return context.db_engine
    return None


async def test_db_connection() -> bool:
    """Test the database connection (async version)"""
    start = time.perf_counter()
    status = "success"
    try:
        await init_engine()
        engine = get_engine()
        if engine is None:
            raise RuntimeError("database engine is not initialized")
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _logger.debug("Database connection test succeeded")
        return True
    except Exception as e:
        status = "error"
        _logger.error("Database connection failed: %s", e)
        return False
    finally:
        from core.observability.metrics import DB_HEALTH_STATE, DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_HEALTH_STATE.labels("healthy").set(1 if status == "success" else 0)
        DB_HEALTH_STATE.labels("unhealthy").set(1 if status == "error" else 0)
        DB_SESSION_TOTAL.labels("healthcheck", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("healthcheck", status).observe(time.perf_counter() - start)
