import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from core.config import Config

_logger = logging.getLogger(__name__)

Base = declarative_base()

_engine = None
_session_factory = None


def get_engine():
    """获取数据库引擎（单例）"""
    global _engine
    if _engine is None:
        _logger.info(f"[DB] 正在初始化数据库连接池: {Config.DATABASE_URL.split('@')[-1]}")
        _engine = create_engine(
            Config.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,  # 连接前检查有效性
            pool_size=Config.DB_POOL_SIZE,  # 连接池大小
            max_overflow=Config.DB_MAX_OVERFLOW,  # 最大溢出连接
            pool_recycle=Config.DB_POOL_RECYCLE,  # 连接回收时间
            pool_timeout=Config.DB_POOL_TIMEOUT  # 连接超时
        )
    return _engine


def get_session():
    """获取数据库会话"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine())
    return _session_factory()


@contextmanager
def session_scope():
    """数据库会话上下文管理器，自动处理提交和回滚"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception: # noqa: PERF203
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """初始化数据库表"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _logger.info("数据库表初始化完成")


def test_db_connection() -> bool:
    """
    测试数据库连接

    Returns:
        bool: 连接成功返回 True，失败返回 False
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        _logger.info("数据库连接测试成功")
        return True
    except Exception as e: # noqa: PERF203
        _logger.error(f"数据库连接失败: {e}")
        return False
