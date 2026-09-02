from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class InboundRule(Base):
    """What to do with an alert on the way IN, decided by the same matcher as forwarding.

    Forwarding has always been rule-driven — a table, a priority order, a match
    vocabulary, an operator UI. The inbound side grew the opposite way: each
    decision got its own storage, and the newest of them (which alerts are never
    worth a model call) was a comma-separated environment variable.

    The match vocabulary here is deliberately identical to ForwardRule's, because
    `_rule_matches` is shared: silences already probe it by building a forward
    rule snapshot. What differs is the verb. A forward rule names a target; an
    inbound rule names an `action`.
    """

    __tablename__ = "inbound_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    match_event_type: Mapped[str] = mapped_column(String(200), default="")
    match_importance: Mapped[str] = mapped_column(String(50), default="")
    match_duplicate: Mapped[str] = mapped_column(String(20), default="all")
    match_source: Mapped[str] = mapped_column(String(200), default="")
    match_project: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_region: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_environment: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_payload: Mapped[str] = mapped_column(String(512), default="")
    # Not part of the shared vocabulary: the alert RULE name, which is what an
    # operator actually thinks in ("stop analysing 示例充值超限告警"). Exact,
    # comma-separated, case-insensitive.
    match_rule_name: Mapped[str] = mapped_column(String(512), default="", server_default="")

    # skip_ai | skip_deep_analysis | cap_importance | digest. Deliberately not
    # "drop" or "mute": an alert that is never stored cannot be investigated
    # afterwards, and muting already has a home in silences, with expiry
    # semantics this table does not have.
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    # What the verb acts ON, when it needs a target: cap_importance stores the
    # ceiling here, digest the window in minutes. Empty for verbs that need
    # none (skip_ai, skip_deep_analysis).
    action_value: Mapped[str] = mapped_column(String(20), default="", server_default="")

    comment: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow())

    __table_args__ = (Index("idx_inbound_rules_priority", "priority"),)
