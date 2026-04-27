import asyncio
import logging
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from core.config import Config

_logger = logging.getLogger(__name__)

Base = declarative_base()

_engine = None
_session_factory = None
_engine_loop = None  # 记录引擎绑定的事件循环

# 为了让原本依赖同步执行的命令（如 migrations 和 cli scripts）可以平滑过渡，
# 我们可以创建一个同步的 engine


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
        pool_timeout=Config.DB_POOL_TIMEOUT
    )

def get_engine():
    """获取异步数据库引擎（单例，自动跟踪事件循环）

    如果当前事件循环与引擎创建时的事件循环不同，
    会自动丢弃旧引擎并创建新引擎，防止跨事件循环使用连接。
    """
    global _engine, _session_factory, _engine_loop

    current_loop = None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    # 如果事件循环发生变化，需要重建引擎和 session 工厂
    if _engine is not None and current_loop is not None and _engine_loop is not current_loop:
        _logger.warning("[DB] 检测到事件循环变更，正在重建异步引擎和 session 工厂")
        _engine = None
        _session_factory = None
        _engine_loop = None

    if _engine is None:
        _engine = _create_engine()
        _engine_loop = current_loop
    return _engine

def get_sync_engine():
    """获取同步数据库引擎，主要用于脚本或 DDL 初始化"""
    global _sync_engine
    if _sync_engine is None:
        sync_url = Config.DATABASE_URL.replace("+asyncpg", "", 1)
        _sync_engine = create_engine(sync_url, echo=False)
    return _sync_engine

def get_session() -> AsyncSession:
    """获取异步数据库会话"""
    global _session_factory
    engine = get_engine()  # 先调用 get_engine 触发事件循环检查
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    return _session_factory()

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
    """关闭并清理异步引擎连接池（用于应用关闭时调用）"""
    global _engine, _session_factory, _engine_loop
    if _engine is not None:
        _logger.info("[DB] 正在关闭异步数据库连接池")
        await _engine.dispose()
        _engine = None
        _session_factory = None
        _engine_loop = None

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
