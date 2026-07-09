"""audit_log table — team activity feed

Revision ID: 0010_audit_log
Revises: 0009_incidents
Create Date: 2026-07-09 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_audit_log"
down_revision: str | Sequence[str] | None = "0009_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("resource_name", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_type_created", "audit_log", ["resource_type", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_type_created", table_name="audit_log")
    op.drop_table("audit_log")
