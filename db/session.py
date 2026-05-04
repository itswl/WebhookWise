import logging
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from core.config import Config
from core.utils import mask_url

_logger = logging.getLogger(__name__)

Base = declarative_base()

# ── 全局异步引擎（主事件循环使用） ──
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _build_engine_kwargs():
    """返回连接池公共参数"""
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": Config.db.DB_POOL_SIZE,
        "max_overflow": Config.db.DB_MAX_OVERFLOW,
        "pool_recycle": Config.db.DB_POOL_RECYCLE,
        "pool_timeout": Config.db.DB_POOL_TIMEOUT,
        "connect_args": {
            "server_settings": {
                "statement_timeout": str(Config.db.DB_STATEMENT_TIMEOUT_MS),
                # 关闭同步提交可提升写入 3-5x，断电时可能丢失最近 1-2s 数据
                # Webhook 场景可接受（有 Redis MQ + RecoveryPoller 兜底）
                "synchronous_commit": Config.db.DB_SYNC_COMMIT,
            }
        },
    }


def _async_url() -> str:
    """将 DATABASE_URL 的 driver 前缀安全替换为 asyncpg。

    不使用 make_url 解析，避免密码含 @#%: 等特殊字符时被误判为 URL 分隔符。
    """
    url = Config.db.DATABASE_URL
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+asyncpg://", 1)
    return url


# ────────────────────────────────────────
# 公共 API
# ────────────────────────────────────────


async def init_engine():
    """创建全局 AsyncEngine 和 async_sessionmaker（应用启动时调用一次）"""
    global _engine, _session_factory
    _logger.info(f"[DB] 正在初始化异步数据库连接池: {mask_url(Config.db.DATABASE_URL)}")
    _engine = create_async_engine(_async_url(), **_build_engine_kwargs())
    _session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        from core.otel import instrument_sqlalchemy

        instrument_sqlalchemy(_engine.sync_engine)
    except Exception:
        pass
    _setup_pool_metrics(_engine)


def _setup_pool_metrics(engine: AsyncEngine):
    """注册连接池事件监听，通过回调更新 Prometheus Gauge。"""
    from core.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE

    pool = engine.sync_engine.pool

    @event.listens_for(pool, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):
        DB_POOL_CHECKED_OUT.inc()

    @event.listens_for(pool, "checkin")
    def _on_checkin(dbapi_conn, connection_record):
        DB_POOL_CHECKED_OUT.dec()

    # 初始化连接池容量
    DB_POOL_SIZE.set(pool.size() + pool.overflow())
    _logger.info("[DB] Pool 事件监听已注册 (checkout/checkin → Prometheus Gauge)")


async def dispose_engine():
    """关闭全局异步引擎（应用关闭时调用）"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    _logger.info("[DB] 当前数据库引擎已关闭")


def get_engine() -> AsyncEngine | None:
    """返回全局异步引擎（向后兼容）"""
    return _engine


async def get_db_session():
    """FastAPI Depends 异步生成器：提供带自动 commit/rollback 的 session"""
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope(existing_session: AsyncSession | None = None):
    """异步数据库会话上下文管理器，自动处理提交和回滚。

    如果传入了 existing_session，则直接使用它且不执行自动提交/回滚（由调用方负责）。
    始终创建新 session（若未传入）。供 Poller、TaskIQ 等不通过路由的代码路径使用。
    """
    if existing_session:
        yield existing_session
    else:
        async with _session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


async def count_with_timeout(
    session: AsyncSession,
    stmt,
    timeout_ms: int = 2000,
) -> int | None:
    """带 statement_timeout 兜底的 COUNT 查询（PostgreSQL-only）。

    超时返回 None，调用方应适配 None 场景。
    """
    try:
        # Try to set statement_timeout (PostgreSQL-specific)
        with contextlib.suppress(Exception):
            await session.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))

        result = await session.execute(stmt)
        return result.scalar() or 0
    except Exception as e:
        _logger.warning("COUNT query timeout (%dms): %s", timeout_ms, e)
        # 关键：清理已中止的事务，避免后续查询连带失败
        with contextlib.suppress(Exception):
            await session.rollback()
        return None
    finally:
        # Try to reset statement_timeout (PostgreSQL-specific)
        with contextlib.suppress(Exception):
            await session.execute(text("RESET statement_timeout"))


# ────────────────────────────────────────
# 异步初始化 & 连接测试
# ────────────────────────────────────────


async def init_db():
    """初始化数据库表（异步版本）"""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _logger.info("数据库表初始化完成")


async def test_db_connection() -> bool:
    """测试数据库连接（异步版本）"""
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        _logger.error(f"数据库连接失败: {e}")
        return False
