import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from core.config import Config

_logger = logging.getLogger(__name__)

Base = declarative_base()

# Per-loop 异步引擎存储：{event_loop: (engine, session_factory)}
_loop_to_engine: WeakKeyDictionary = WeakKeyDictionary()
_engine_lock = threading.Lock()

# 同步引擎（独立，不受事件循环影响）
_sync_engine = None


def _create_engine():
    """内部方法：创建新的异步引擎实例"""
    _logger.info(f"[DB] 正在初始化异步数据库连接池: {Config.DATABASE_URL.split('@')[-1]}")
    return create_async_engine(
        Config.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
        echo=False,
        pool_pre_ping=True,
        pool_size=Config.DB_POOL_SIZE,
        max_overflow=Config.DB_MAX_OVERFLOW,
        pool_recycle=Config.DB_POOL_RECYCLE,
        pool_timeout=Config.DB_POOL_TIMEOUT,
    )


def _get_or_create(current_loop):
    """获取或创建当前事件循环对应的 (engine, session_factory) 元组（需在 _engine_lock 内调用）"""
    if current_loop not in _loop_to_engine:
        engine = _create_engine()
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        _loop_to_engine[current_loop] = (engine, factory)
    return _loop_to_engine[current_loop]


def get_engine():
    """获取当前事件循环对应的异步引擎（per-loop 隔离，线程安全）"""
    current_loop = asyncio.get_running_loop()
    with _engine_lock:
        return _get_or_create(current_loop)[0]


def get_sync_engine():
    """获取同步数据库引擎，主要用于脚本或 DDL 初始化"""
    global _sync_engine
    if _sync_engine is None:
        sync_url = Config.DATABASE_URL.replace("+asyncpg", "", 1)
        _sync_engine = create_engine(sync_url, echo=False)
    return _sync_engine


def get_session() -> AsyncSession:
    """获取异步数据库会话"""
    current_loop = asyncio.get_running_loop()
    with _engine_lock:
        _, factory = _get_or_create(current_loop)
    return factory()


@asynccontextmanager
async def session_scope():
    """异步数据库会话上下文管理器，自动处理提交和回滚"""
    session = get_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def init_db():
    """使用同步引擎初始化数据库表"""
    engine = get_sync_engine()
    Base.metadata.create_all(engine)
    _logger.info("数据库表初始化完成")


async def dispose_engine():
    """关闭当前事件循环的数据库引擎（应用关闭时调用）"""
    current_loop = asyncio.get_running_loop()
    with _engine_lock:
        entry = _loop_to_engine.pop(current_loop, None)
    if entry:
        engine, _ = entry
        await engine.dispose()
    _logger.info("[DB] 当前数据库引擎已关闭")


def test_db_connection() -> bool:
    """
    使用同步引擎测试数据库连接（因为这个在系统启动的最早期调用，保持简单同步）
    """
    try:
        with get_sync_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e:
        _logger.error(f"数据库连接失败: {e}")
        return False
