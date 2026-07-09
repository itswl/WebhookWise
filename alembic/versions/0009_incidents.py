"""incidents table — auto-group related alerts into operational incidents

Revision ID: 0009_incidents
Revises: 0008_decision_trace_silence_id
Create Date: 2026-07-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_incidents"
down_revision: str | Sequence[str] | None = "0008_decision_trace_silence_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("top_importance", sa.String(length=20), nullable=True),
        sa.Column("member_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_status_started", "incidents", ["status", "started_at"])
    op.create_index(
        "ix_incidents_active",
        "incidents",
        ["status"],
        unique=False,
        postgresql_where=text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_active", table_name="incidents")
    op.drop_index("ix_incidents_status_started", table_name="incidents")
    op.drop_table("incidents")
