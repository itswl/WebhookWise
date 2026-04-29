import logging
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from core.config import Config

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
    }


def _async_url() -> str:
    return Config.db.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")


# ────────────────────────────────────────
# 公共 API
# ────────────────────────────────────────


async def init_engine():
    """创建全局 AsyncEngine 和 async_sessionmaker（应用启动时调用一次）"""
    global _engine, _session_factory
    _logger.info(f"[DB] 正在初始化异步数据库连接池: {Config.db.DATABASE_URL.split('@')[-1]}")
    _engine = create_async_engine(_async_url(), **_build_engine_kwargs())
    _session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)


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
async def session_scope():
    """异步数据库会话上下文管理器，自动处理提交和回滚。

    始终创建新 session。供 Poller、BackgroundTasks 等不通过路由的代码路径使用。
    路由端点应使用 Depends(get_db_session) 显式注入 session。
    """
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


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
