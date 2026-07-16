from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class Silence(Base):
    """A manual mute: while active, alerts matching the criteria are NOT forwarded.

    A silence is the deny counterpart to a ForwardRule (which allows/routes). It
    reuses the same match semantics (source/importance/event_type/project/region/
    environment/payload) so "silence source=volcengine,project=eve-cn" behaves
    like rule matching. It only suppresses forwarding/notification — events are
    still ingested, deduplicated, and analyzed.

    Active iff: lifted_at IS NULL AND (expires_at IS NULL OR expires_at > now).
    """

    __tablename__ = "silences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    match_source: Mapped[str] = mapped_column(String(200), default="")
    match_importance: Mapped[str] = mapped_column(String(50), default="")
    match_event_type: Mapped[str] = mapped_column(String(200), default="")
    match_project: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_region: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_environment: Mapped[str] = mapped_column(String(200), default="", server_default="")
    match_payload: Mapped[str] = mapped_column(String(512), default="")

    comment: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    # Nullable = silence stays active until manually lifted.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when an operator lifts the silence (soft state; "active" is derived).
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # Serves the active-silences lookup (the hot path: read on every forward
        # decision). Partial index over not-yet-lifted rows ordered by expiry.
        Index(
            "idx_silences_active",
            "expires_at",
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )


class MaintenanceWindow(Base):
    """A recurring silence schedule (e.g. "every Sunday 02:00–04:00, mute host X").

    The window itself never matches alerts. A scheduler sweep materializes each
    active occurrence into a normal expiring Silence row (created_by =
    "maintenance-window", comment carrying an occurrence marker), so suppression
    accounting, the debt report, and the forward-decision cache all keep working
    on plain silences. Deleting or disabling a window lifts its live silence on
    the next sweep.
    """

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Same match semantics as Silence / ForwardRule.
    match_source: Mapped[str] = mapped_column(String(200), default="")
    match_importance: Mapped[str] = mapped_column(String(50), default="")
    match_event_type: Mapped[str] = mapped_column(String(200), default="")
    match_project: Mapped[str] = mapped_column(String(200), default="")
    match_region: Mapped[str] = mapped_column(String(200), default="")
    match_environment: Mapped[str] = mapped_column(String(200), default="")
    match_payload: Mapped[str] = mapped_column(String(512), default="")

    # Schedule: ISO weekday numbers (1=Monday … 7=Sunday) as a CSV string, a
    # start expressed as minutes after local midnight in `timezone`, and a
    # duration. A window may cross midnight; its occurrence date is the day the
    # window STARTS on.
    days_of_week: Mapped[str] = mapped_column(String(20), nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)

    comment: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow())
