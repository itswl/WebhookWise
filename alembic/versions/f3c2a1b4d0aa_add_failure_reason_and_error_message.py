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
    op.execute(sa.text("ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(500)"))
    op.execute(sa.text("ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS error_message TEXT"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE webhook_events DROP COLUMN IF EXISTS error_message"))
    op.execute(sa.text("ALTER TABLE webhook_events DROP COLUMN IF EXISTS failure_reason"))
