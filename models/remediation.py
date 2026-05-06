import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class RemediationExecution(Base):
    """Runbook 执行记录"""

    __tablename__ = "remediation_execution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    runbook_name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_alert_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_alert_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    steps_log: Mapped[str] = mapped_column(Text, default="[]")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_remediation_status_time", "status", "started_at"),)

    def to_dict(self) -> dict[str, object]:
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
