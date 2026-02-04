"""
数据库模型定义
"""
from datetime import datetime
from contextlib import contextmanager
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, Index, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import Config
import logging

Base = declarative_base()

# 模块 logger
_logger = logging.getLogger(__name__)

# 全局数据库引擎（单例）
_engine = None
_session_factory = None


class WebhookEvent(Base):
    """Webhook 事件模型"""
    __tablename__ = 'webhook_events'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False, index=True)
    client_ip = Column(String(50))
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)
    
    # 原始数据
    raw_payload = Column(Text)
    headers = Column(JSON)
    parsed_data = Column(JSON)
    
    # 告警去重标识 (基于关键字段的哈希值)
    alert_hash = Column(String(64), index=True)
    
    # AI 分析结果
    ai_analysis = Column(JSON)
    importance = Column(String(20), index=True)  # high, medium, low
    
    # 转发状态
    forward_status = Column(String(20))  # success, failed, skipped
    
    # 是否为重复告警
    is_duplicate = Column(Integer, default=0)  # 0: 新告警, 1: 重复告警
    duplicate_of = Column(Integer)  # 如果是重复告警，指向原始告警的ID
    duplicate_count = Column(Integer, default=1)  # 重复次数
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 复合索引：优化去重查询性能
    __table_args__ = (
        Index('idx_hash_timestamp', 'alert_hash', 'timestamp'),
        Index('idx_importance_timestamp', 'importance', 'timestamp'),
        Index('idx_duplicate_lookup', 'alert_hash', 'is_duplicate', 'timestamp'),
    )
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'source': self.source,
            'client_ip': self.client_ip,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'raw_payload': self.raw_payload,
            'headers': self.headers,
            'parsed_data': self.parsed_data,
            'alert_hash': self.alert_hash,
            'ai_analysis': self.ai_analysis,
            'importance': self.importance,
            'forward_status': self.forward_status,
            'is_duplicate': self.is_duplicate,
            'duplicate_of': self.duplicate_of,
            'duplicate_count': self.duplicate_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class ProcessingLock(Base):
    """
    告警处理锁（分布式锁，用于多 worker 环境）
    
    在开始处理告警前插入记录，处理完成后删除。
    利用数据库主键约束防止并发处理同一告警。
    """
    __tablename__ = 'processing_locks'
    
    alert_hash = Column(String(64), primary_key=True)  # 告警哈希作为主键
    created_at = Column(DateTime, default=datetime.now)
    worker_id = Column(String(100))  # 可选：记录哪个 worker 正在处理


# 数据库连接（单例模式）
def get_engine():
    """获取数据库引擎（单例）"""
    global _engine
    if _engine is None:
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """初始化数据库表"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("数据库表初始化完成")


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
    except Exception as e:
        _logger.error(f"数据库连接失败: {e}")
        return False


if __name__ == '__main__':
    init_db()
