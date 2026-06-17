"""add acknowledgement columns to webhook events

Acknowledging an alert ("I'm on it") suppresses its recurring periodic reminder
while leaving the first notification + cooldown intact. Stored on the event so
the decision is durable across worker restarts and visible in the dashboard.
The same columns are mirrored onto archived_webhook_events so the archive keeps
a complete snapshot.

Revision ID: 0005_ack_columns
Revises: 0004_silences
Create Date: 2026-06-17 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_ack_columns"
down_revision: str | Sequence[str] | None = "0004_silences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("webhook_events", sa.Column("acknowledged_by", sa.String(length=100), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("acknowledged_by", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("archived_webhook_events", "acknowledged_by")
    op.drop_column("archived_webhook_events", "acknowledged_at")
    op.drop_column("webhook_events", "acknowledged_by")
    op.drop_column("webhook_events", "acknowledged_at")
