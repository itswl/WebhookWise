"""Refill decision_trace.alert_name rows nulled by a wrong-dict regression

Between 2026-08-03 (deploy of 4d866cb) and the fix, the trace writer read
"name" off the forward-match identity — a dict that only ever carries
project/region/environment — so every new row stored NULL and the per-rule
quality/noise aggregates silently collapsed back to source grain (the exact
coarseness alert_name was added to fix). The write path is corrected in the
same change; this migration re-runs 0025's idempotent backfill (scoped by
`alert_name IS NULL`) so the gap rows recover their names from
webhook_events.parsed_data->'_alert_identity'.

Revision ID: 0026_trace_alert_name_refill
Revises: 0025_trace_alert_name
Create Date: 2026-08-04 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026_trace_alert_name_refill"
down_revision: str | Sequence[str] | None = "0025_trace_alert_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE decision_trace AS t
           SET alert_name = left(e.parsed_data -> '_alert_identity' ->> 'name', 200)
        FROM webhook_events AS e
        WHERE e.id = t.webhook_event_id
          AND t.alert_name IS NULL
          AND e.parsed_data -> '_alert_identity' ->> 'name' IS NOT NULL
        """
    )


def downgrade() -> None:
    # Data refill; nothing sensible to undo (0025 owns the column itself).
    pass
