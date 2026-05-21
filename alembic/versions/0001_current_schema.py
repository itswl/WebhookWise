"""current schema baseline

Revision ID: 0001_current_schema
Revises:
Create Date: 2026-05-21 00:00:00.000000
"""

from collections.abc import Sequence

import models  # noqa: F401  # ensure model metadata is registered
from alembic import op
from db.session import Base

revision: str = "0001_current_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
