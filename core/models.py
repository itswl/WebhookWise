"""
数据库模型定义
"""
from datetime import datetime
from contextlib import contextmanager
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, Index, text, Float, Boolean, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from core.config import Config
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
    beyond_window = Column(Integer, default=0)  # 0: 窗口内, 1: 窗口外
    last_notified_at = Column(DateTime)  # 上次通知时间（用于周期性提醒）

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 复合索引：优化去重查询性能
    __table_args__ = (
        Index('idx_hash_timestamp', 'alert_hash', 'timestamp'),
        Index('idx_importance_timestamp', 'importance', 'timestamp'),
        Index('idx_duplicate_lookup', 'alert_hash', 'is_duplicate', 'timestamp'),
    )
    
    def to_summary_dict(self):
        """返回摘要信息（用于列表显示，减少数据传输量）"""
        # 提取 AI 分析摘要
        summary = None
        if self.ai_analysis:
            summary = self.ai_analysis.get('summary', '')

        # 提取关键告警信息
        alert_info = {}
        if self.parsed_data:
            # 根据不同来源提取关键字段
            if self.source == 'mongodb':
                alert_info = {
                    'host': self.parsed_data.get('监控项', {}).get('主机', '') if isinstance(self.parsed_data.get('监控项'), dict) else '',
                    'metric': self.parsed_data.get('监控项', {}).get('监控项', '') if isinstance(self.parsed_data.get('监控项'), dict) else '',
                    'value': self.parsed_data.get('当前值', '')
                }
            # 可以添加其他来源的提取逻辑

        return {
            'id': self.id,
            'source': self.source,
            'client_ip': self.client_ip,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'importance': self.importance,
            'is_duplicate': self.is_duplicate,
            'duplicate_of': self.duplicate_of,
            'duplicate_count': self.duplicate_count,
            'beyond_window': self.beyond_window,
            'forward_status': self.forward_status,
            'summary': summary,  # AI 摘要（而非完整分析）
            'alert_info': alert_info,  # 告警关键信息（而非完整 parsed_data）
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'prev_alert_id': None,  # 占位符，由 get_all_webhooks 填充
            'prev_alert_timestamp': None  # 占位符，由 get_all_webhooks 填充
        }

    def to_dict(self):
        """转换为字典（完整数据，用于详情查看）"""
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
            'beyond_window': self.beyond_window,
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


class AnalysisCache(Base):
    """
    AI 分析结果缓存
    
    用于存储 AI 分析结果，避免对相同告警重复调用 AI。
    缓存 key 基于 alert_hash 生成。
    """
    __tablename__ = 'analysis_cache'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(128), unique=True, nullable=False, index=True)  # 基于 alert_hash
    analysis_result = Column(Text, nullable=False)  # JSON 格式的分析结果
    hit_count = Column(Integer, default=0)  # 缓存命中次数
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)
    
    def is_expired(self) -> bool:
        """检查缓存是否已过期"""
        return datetime.now() > self.expires_at if self.expires_at else True


class AIUsageLog(Base):
    """
    AI 调用成本追踪
    
    记录每次 AI 分析的调用信息，用于成本追踪和用量统计。
    """
    __tablename__ = 'ai_usage_log'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=func.now(), index=True)
    model = Column(String(100))  # 使用的模型名称
    tokens_in = Column(Integer, default=0)  # 输入 token 数
    tokens_out = Column(Integer, default=0)  # 输出 token 数
    cost_estimate = Column(Float, default=0.0)  # 估算成本（美元）
    cache_hit = Column(Boolean, default=False)  # 是否命中缓存
    route_type = Column(String(20))  # 'ai', 'rule', 'cache'
    alert_hash = Column(String(64), index=True)  # 关联的告警哈希
    source = Column(String(100))  # 告警来源
    
    # 复合索引：优化统计查询性能
    __table_args__ = (
        Index('idx_usage_timestamp_route', 'timestamp', 'route_type'),
    )


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



class AlertCorrelation(Base):
    """
    告警关联模型
    
    记录告警之间的关联关系和共现统计，用于优化根因分析。
    """
    __tablename__ = 'alert_correlation'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_hash_a = Column(String(64), nullable=False, index=True)
    alert_hash_b = Column(String(64), nullable=False, index=True)
    co_occurrence_count = Column(Integer, default=1)  # 共同出现次数
    avg_time_delta = Column(Float, default=0.0)  # 平均时间差（秒），A 在 B 之前为正
    confidence = Column(Float, default=0.0)  # 关联置信度 0-1
    last_seen = Column(DateTime, default=func.now())
    created_at = Column(DateTime, default=func.now())
    
    __table_args__ = (
        # 唯一约束：同一对告警哈希不重复
        Index('idx_alert_correlation_unique', 'alert_hash_a', 'alert_hash_b', unique=True),
        Index('idx_alert_correlation_lookup', 'alert_hash_a', 'alert_hash_b'),
        {'extend_existing': True}
    )


class RemediationExecution(Base):
    """
    Runbook 执行记录
    
    记录每次 Runbook 执行的详细信息，包括执行状态、步骤日志等。
    """
    __tablename__ = 'remediation_execution'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(String(64), unique=True, nullable=False, index=True)  # UUID
    runbook_name = Column(String(200), nullable=False)
    trigger_alert_id = Column(Integer, nullable=True)  # 关联 WebhookEvent.id
    trigger_alert_hash = Column(String(64))
    status = Column(String(30), default='pending')  # pending/awaiting_approval/running/success/failed/rolled_back/dry_run_complete
    steps_log = Column(Text, default='[]')  # JSON 格式的执行步骤日志
    dry_run = Column(Boolean, default=False)
    started_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # 索引：优化按状态和时间查询
    __table_args__ = (
        Index('idx_remediation_status_time', 'status', 'started_at'),
    )
    
    def to_dict(self):
        """转换为字典"""
        import json
        return {
            'id': self.id,
            'execution_id': self.execution_id,
            'runbook_name': self.runbook_name,
            'trigger_alert_id': self.trigger_alert_id,
            'trigger_alert_hash': self.trigger_alert_hash,
            'status': self.status,
            'steps_log': json.loads(self.steps_log) if self.steps_log else [],
            'dry_run': self.dry_run,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message
        }


class Prediction(Base):
    """
    预测结果模型
    
    存储预测引擎生成的异常检测、趋势预测、风暴预警结果。
    """
    __tablename__ = 'prediction'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_type = Column(String(50), nullable=False, index=True)  # anomaly/trend/storm
    target = Column(String(200))  # 预测目标（如 source 名称）
    predicted_value = Column(Float)
    confidence = Column(Float, default=0.0)
    details = Column(Text, default='{}')  # JSON 格式详细信息
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    
    # 索引：优化按类型和时间查询
    __table_args__ = (
        Index('idx_prediction_type_created', 'prediction_type', 'created_at'),
    )
    
    def to_dict(self):
        """转换为字典"""
        import json
        return {
            'id': self.id,
            'prediction_type': self.prediction_type,
            'target': self.target,
            'predicted_value': self.predicted_value,
            'confidence': self.confidence,
            'details': json.loads(self.details) if self.details else {},
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def is_expired(self) -> bool:
        """检查预测是否已过期"""
        if not self.expires_at:
            return False
        return datetime.now() > self.expires_at


class ForwardRule(Base):
    """转发规则配置"""
    __tablename__ = 'forward_rules'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)           # 规则名称
    enabled = Column(Boolean, default=True)              # 是否启用
    priority = Column(Integer, default=0)                # 规则优先级（越大越先匹配）
    
    # 匹配条件
    match_importance = Column(String(50), default='')    # high,medium,low（逗号分隔，空=全部匹配）
    match_duplicate = Column(String(20), default='all')  # new/duplicate/beyond_window/all
    match_source = Column(String(200), default='')       # 来源匹配（逗号分隔列表，空=全部）
    
    # 转发目标
    target_type = Column(String(20), nullable=False)     # feishu / openocta / webhook
    target_url = Column(String(500), default='')         # 飞书/webhook 的 URL
    target_name = Column(String(100), default='')        # 显示名称
    
    # 行为配置
    stop_on_match = Column(Boolean, default=False)       # 匹配后是否停止后续规则
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'enabled': self.enabled,
            'priority': self.priority,
            'match_importance': self.match_importance,
            'match_duplicate': self.match_duplicate,
            'match_source': self.match_source,
            'target_type': self.target_type,
            'target_url': self.target_url,
            'target_name': self.target_name,
            'stop_on_match': self.stop_on_match,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


if __name__ == '__main__':
    init_db()
