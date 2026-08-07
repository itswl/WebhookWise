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


class ImportanceOverride(Base):
    """An operator's importance correction, applied to this condition from now on.

    This is what closes the loop. Before it, correcting an alert changed that
    one row and nothing else: the same condition fired an hour later and the
    model called it `low` again, because nothing anywhere read the correction.

    Keyed on alert_hash — the identity a condition keeps across occurrences —
    so one correction covers every future firing of the same thing and nothing
    else. Deliberately NOT few-shot in the prompt: with a handful of samples
    that teaches nothing, and with many it would quietly move judgements on
    alerts nobody corrected, in a way no one could trace back.

    hit_count is not decoration. It answers the only question that matters
    about an override — is it still earning its place, or is it a rule somebody
    set once and forgot.
    """

    __tablename__ = "importance_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    importance: Mapped[str] = mapped_column(String(20), nullable=False)
    # Carried for the management list: an alert_hash alone is unreadable.
    source: Mapped[str | None] = mapped_column(String(100))
    alert_name: Mapped[str | None] = mapped_column(String(200))
    origin_event_id: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(100), default="dashboard", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)


class WorkflowTransition(Base):
    """One reversible operator workflow change, kept so it can be taken back.

    The audit log records what a change BECAME, in prose. That is enough to
    read the history and not enough to reverse it — undoing needs the prior
    values, structurally, and it needs to know whether anything has happened
    since. Both live here.

    Same shape and same safety rule as NoiseReductionAction: an undo applies
    only while the resource still matches `after_state`, so taking back your
    own misclick can never quietly discard somebody else's later decision.
    """

    __tablename__ = "workflow_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # "webhook_event" | "incident"
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    before_state: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    after_state: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)
    # "applied" | "undone"
    status: Mapped[str] = mapped_column(String(20), default="applied", nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="dashboard", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        # The undo path always asks the same question: what was the most recent
        # still-applied change to THIS resource?
        Index("ix_workflow_transitions_resource", "resource_type", "resource_id", "id"),
    )


class RuntimeSetting(Base):
    """One live override for an operator-policy config key.

    Sparse by design: a missing row means "use the env value / default".
    Values are stored as strings and validated against the setting registry
    (services/operations/runtime_settings.py) on write.
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow())
