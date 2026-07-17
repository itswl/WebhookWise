"""index previous-alert references used by safe archival

The archival selector preserves events that are still referenced by a live
flapping chain. This index keeps its correlated dependency check from scanning
the webhook event table once per candidate.

Revision ID: 0018_archival_dependency_index
Revises: 0017_maintenance_windows
Create Date: 2026-07-17 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_archival_dependency_index"
down_revision: str | Sequence[str] | None = "0017_maintenance_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_webhook_events_prev_alert_id"),
        "webhook_events",
        ["prev_alert_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_events_prev_alert_id"), table_name="webhook_events")
