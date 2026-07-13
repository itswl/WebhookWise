"""Operator workflow notes and human analysis feedback."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class OperationalNote(Base):
    """A durable operator note attached to an alert or incident."""

    __tablename__ = "operational_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="operator", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)

    __table_args__ = (Index("ix_operational_notes_resource", "resource_type", "resource_id", "created_at"),)


class AnalysisFeedback(Base):
    """Human feedback used to measure and improve analysis quality."""

    __tablename__ = "analysis_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    corrected_importance: Mapped[str | None] = mapped_column(String(20))
    corrected_event_type: Mapped[str | None] = mapped_column(String(100))
    comment: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="operator", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)

    __table_args__ = (
        Index("ix_analysis_feedback_resource", "resource_type", "resource_id", "created_at"),
        Index("ix_analysis_feedback_verdict_created", "verdict", "created_at"),
    )


class NoiseReductionAction(Base):
    """A durable, reversible optimization applied from the noise center."""

    __tablename__ = "noise_reduction_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suggestion_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(Integer)
    before_state: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    after_state: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    estimated_notifications: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="applied", nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="operator", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_noise_reduction_actions_status_created", "status", "created_at"),)
