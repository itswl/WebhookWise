"""Add server_default to webhook_events timestamps.

Revision ID: 0003_server_defaults
Revises: 0002_forward_rule_identity_match
Create Date: 2026-06-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_server_defaults"
down_revision: str | Sequence[str] | None = "0002_forward_rule_identity_match"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("webhook_events", "created_at", server_default=sa.text("now()"))
    op.alter_column("webhook_events", "updated_at", server_default=sa.text("now()"))


def downgrade() -> None:
    op.alter_column("webhook_events", "updated_at", server_default=None)
    op.alter_column("webhook_events", "created_at", server_default=None)
