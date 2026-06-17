"""add silences table (manual mute / snooze)

A silence suppresses forwarding for matching alerts while active. Mirrors the
ForwardRule match columns; active iff lifted_at IS NULL AND (expires_at IS NULL
OR expires_at > now).

Revision ID: 0004_silences
Revises: 0003_archived_dedup_key
Create Date: 2026-06-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0004_silences"
down_revision: str | Sequence[str] | None = "0003_archived_dedup_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "silences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_source", sa.String(length=200), nullable=False),
        sa.Column("match_importance", sa.String(length=50), nullable=False),
        sa.Column("match_event_type", sa.String(length=200), nullable=False),
        sa.Column("match_project", sa.String(length=200), server_default="", nullable=False),
        sa.Column("match_region", sa.String(length=200), server_default="", nullable=False),
        sa.Column("match_environment", sa.String(length=200), server_default="", nullable=False),
        sa.Column("match_payload", sa.String(length=512), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("lifted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_silences_active",
        "silences",
        ["expires_at"],
        unique=False,
        postgresql_where=text("lifted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_silences_active", table_name="silences")
    op.drop_table("silences")
