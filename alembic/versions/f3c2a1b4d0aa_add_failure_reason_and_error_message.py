"""add failure_reason and error_message to webhook_events

Revision ID: f3c2a1b4d0aa
Revises: e1a4f7b3d926
Create Date: 2026-05-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3c2a1b4d0aa"
down_revision: str | Sequence[str] | None = "e1a4f7b3d926"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("failure_reason", sa.String(length=500), nullable=True))
    op.add_column("webhook_events", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("webhook_events", "error_message")
    op.drop_column("webhook_events", "failure_reason")

