import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import create_engine, text
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

# ── ContextVar: Depends 注入的 session 向下传播 ──
_request_session: ContextVar[AsyncSession | None] = ContextVar("_request_session", default=None)


def _build_engine_kwargs():
    """返回连接池公共参数"""
    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": Config.DB_POOL_SIZE,
        "max_overflow": Config.DB_MAX_OVERFLOW,
        "pool_recycle": Config.DB_POOL_RECYCLE,
        "pool_timeout": Config.DB_POOL_TIMEOUT,
    }


def _async_url() -> str:
    return Config.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")


# ────────────────────────────────────────
# 公共 API
# ────────────────────────────────────────


async def init_engine():
    """创建全局 AsyncEngine 和 async_sessionmaker（应用启动时调用一次）"""
    global _engine, _session_factory
    _logger.info(f"[DB] 正在初始化异步数据库连接池: {Config.DATABASE_URL.split('@')[-1]}")
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
        token = _request_session.set(session)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            _request_session.reset(token)


@asynccontextmanager
async def session_scope():
    """异步数据库会话上下文管理器，自动处理提交和回滚。

    优先复用 Depends 注入的 session；否则用全局 factory 创建新 session。
    """
    # 1. 优先复用 Depends 注入的 session
    existing = _request_session.get()
    if existing:
        yield existing
        return
    # 2. 用全局 factory 创建新 session
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ────────────────────────────────────────
# 同步引擎（仅供 migrations 脚本使用）
# ────────────────────────────────────────


def get_sync_engine():
    """获取同步数据库引擎，仅供 migrations/ 脚本使用"""
    sync_url = Config.DATABASE_URL.replace("+asyncpg", "", 1)
    return create_engine(sync_url, echo=False)


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
