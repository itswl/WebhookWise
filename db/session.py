import logging
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from core.config import Config

_logger = logging.getLogger(__name__)

Base = declarative_base()

_engine = None
_session_factory = None

# 为了让原本依赖同步执行的命令（如 migrations 和 cli scripts）可以平滑过渡，
# 我们可以创建一个同步的 engine
from sqlalchemy import create_engine

_sync_engine = None

def get_engine():
    """获取异步数据库引擎（单例）"""
    global _engine
    if _engine is None:
        _logger.info(f"[DB] 正在初始化异步数据库连接池: {Config.DATABASE_URL.split('@')[-1]}")
        _engine = create_async_engine(
            Config.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=Config.DB_POOL_SIZE,
            max_overflow=Config.DB_MAX_OVERFLOW,
            pool_recycle=Config.DB_POOL_RECYCLE,
            pool_timeout=Config.DB_POOL_TIMEOUT
        )
    return _engine

def get_sync_engine():
    """获取同步数据库引擎，主要用于脚本或 DDL 初始化"""
    global _sync_engine
    if _sync_engine is None:
        sync_url = Config.DATABASE_URL.replace('+asyncpg', '')
        _sync_engine = create_engine(sync_url, echo=False)
    return _sync_engine

def get_session() -> AsyncSession:
    """获取异步数据库会话"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
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
