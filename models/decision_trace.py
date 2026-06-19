from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class DecisionTrace(Base):
    """A queryable record of why a webhook event was forwarded or skipped.

    Each processed alert produces one trace: the ordered chain of decision steps
    (dedup → silence → noise → analysis → rule match → forward) plus the
    flattened outcome/skip_code so the dashboard can both aggregate ("how many
    were silenced / cooled-down / forwarded") and show the full per-alert "why".

    Written in the same transaction as the event persist, so a trace never
    outlives or precedes its event; trace-write failure degrades gracefully and
    must not block the forward decision itself.
    """

    __tablename__ = "decision_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    webhook_event_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), index=True)

    # Flattened, indexed summary for cheap GROUP BY aggregation.
    outcome: Mapped[str] = mapped_column(String(20), index=True)  # forwarded | skipped
    skip_code: Mapped[str] = mapped_column(String(40), default="none", index=True)

    source: Mapped[str | None] = mapped_column(String(100))
    importance: Mapped[str | None] = mapped_column(String(20))
    is_periodic_reminder: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    # Names of the forward rules that matched (for the per-alert detail view).
    matched_rules: Mapped[list[str] | None] = mapped_column(JSONB)
    # Ordered decision chain: [{"step": ..., "result": ..., ...}, ...]
    steps: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
