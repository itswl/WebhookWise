"""drop acknowledgement columns

The alert-acknowledgement feature was removed (silence covers the mute use
case). Drop the acknowledged_at/acknowledged_by columns from webhook_events and
archived_webhook_events.

Revision ID: 0006_drop_ack_columns
Revises: 0005_ack_columns
Create Date: 2026-06-17 00:00:02.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_drop_ack_columns"
down_revision: str | Sequence[str] | None = "0005_ack_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("webhook_events", "acknowledged_by")
    op.drop_column("webhook_events", "acknowledged_at")
    op.drop_column("archived_webhook_events", "acknowledged_by")
    op.drop_column("archived_webhook_events", "acknowledged_at")


def downgrade() -> None:
    op.add_column("archived_webhook_events", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("acknowledged_by", sa.String(length=100), nullable=True))
    op.add_column("webhook_events", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("webhook_events", sa.Column("acknowledged_by", sa.String(length=100), nullable=True))
