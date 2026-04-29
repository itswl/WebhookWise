"""Add prev_alert_id column to webhook_events.

Revision ID: b4c8d2e6f1a3
Revises: a3f7e2b1c9d4
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "b4c8d2e6f1a3"
down_revision = "a3f7e2b1c9d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    conn = op.get_bind()
    insp = inspect(conn)
    columns = [c["name"] for c in insp.get_columns("webhook_events")]

    if "prev_alert_id" not in columns:
        op.add_column("webhook_events", sa.Column("prev_alert_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("webhook_events", "prev_alert_id")
