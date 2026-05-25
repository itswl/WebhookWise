from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class SuppressedRecord(Base):
    __tablename__ = "suppressed_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), default="standalone")
    root_cause_event_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500), default="")
    related_alert_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow())

    __table_args__ = (
        Index("idx_suppressed_records_created_at", "created_at"),
        Index("idx_suppressed_records_hash_created", "alert_hash", "created_at"),
    )
