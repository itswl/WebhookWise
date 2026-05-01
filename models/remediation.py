import json

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)

from db.session import Base


class RemediationExecution(Base):
    """Runbook 执行记录"""

    __tablename__ = "remediation_execution"

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(String(64), unique=True, nullable=False, index=True)
    runbook_name = Column(String(200), nullable=False)
    trigger_alert_id = Column(Integer, nullable=True)
    trigger_alert_hash = Column(String(64))
    status = Column(String(30), default="pending")
    steps_log = Column(Text, default="[]")
    dry_run = Column(Boolean, default=False)
    started_at = Column(DateTime, default=func.now())
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (Index("idx_remediation_status_time", "status", "started_at"),)

    def to_dict(self):
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "runbook_name": self.runbook_name,
            "trigger_alert_id": self.trigger_alert_id,
            "trigger_alert_hash": self.trigger_alert_hash,
            "status": self.status,
            "steps_log": json.loads(self.steps_log) if self.steps_log else [],
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
        }
