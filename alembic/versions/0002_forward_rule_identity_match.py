"""add forward rule identity match fields

Revision ID: 0002_forward_rule_identity_match
Revises: 0001_current_schema
Create Date: 2026-05-28 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0002_forward_rule_identity_match"
down_revision: str | Sequence[str] | None = "0001_current_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIELDS = ("match_project", "match_region", "match_environment")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for field in _FIELDS:
            op.execute(f"ALTER TABLE forward_rules ADD COLUMN IF NOT EXISTS {field} VARCHAR(200)")
            op.execute(f"UPDATE forward_rules SET {field} = '' WHERE {field} IS NULL")
            op.execute(f"ALTER TABLE forward_rules ALTER COLUMN {field} SET DEFAULT ''")
        return

    existing = {column["name"] for column in inspect(bind).get_columns("forward_rules")}
    for field in _FIELDS:
        if field not in existing:
            op.add_column("forward_rules", sa.Column(field, sa.String(length=200), nullable=True))
        op.execute(sa.text(f"UPDATE forward_rules SET {field} = '' WHERE {field} IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in inspect(bind).get_columns("forward_rules")}
    for field in reversed(_FIELDS):
        if field in existing:
            op.drop_column("forward_rules", field)
