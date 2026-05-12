"""add webhook request id

Revision ID: b9e2c4d5f6a7
Revises: a8d1f2c3b4e5
Create Date: 2026-05-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9e2c4d5f6a7"
down_revision: str | Sequence[str] | None = "a8d1f2c3b4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET lock_timeout = '5s'"))
    op.add_column("webhook_events", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.create_index("ix_webhook_events_request_id", "webhook_events", ["request_id"], unique=True)


def downgrade() -> None:
    op.execute(sa.text("SET lock_timeout = '5s'"))
    op.drop_index("ix_webhook_events_request_id", table_name="webhook_events")
    op.drop_column("webhook_events", "request_id")
