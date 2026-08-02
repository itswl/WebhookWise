"""decision_trace.alert_name: aggregate quality by alert rule, not by ecosystem

`source` answers "which system sent this" (grafana, prometheus), which is far
too coarse to judge AI quality: a whole estate collapses into one bucket. The
actionable dimension is the alert RULE — "datasourcenodata is always low, so
silence it" is a decision you can only reach per rule.

The name already exists in webhook_events.parsed_data->'_alert_identity', but
this table flattens dimensions on purpose so aggregates never unpack JSONB.
Existing rows are backfilled from that identity.

Revision ID: 0025_trace_alert_name
Revises: 0024_runtime_settings
Create Date: 2026-08-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_trace_alert_name"
down_revision: str | Sequence[str] | None = "0024_runtime_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decision_trace", sa.Column("alert_name", sa.String(length=200), nullable=True))
    # Leads with alert_name because every consumer filters a created_at window
    # and groups by the rule; partial because unidentified payloads carry NULL.
    op.create_index(
        "ix_decision_trace_alert_name_created_at",
        "decision_trace",
        ["alert_name", "created_at"],
        postgresql_where=sa.text("alert_name IS NOT NULL"),
    )
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
    op.drop_index("ix_decision_trace_alert_name_created_at", table_name="decision_trace")
    op.drop_column("decision_trace", "alert_name")
