"""add decision_trace table (why an alert was forwarded or skipped)

One row per processed alert: the ordered decision chain (dedup → silence →
noise → analysis → rule match → forward) as JSONB, plus a flattened, indexed
outcome/skip_code for cheap aggregation. Written in the same transaction as the
event persist.

Revision ID: 0005_decision_trace
Revises: 0004_silences
Create Date: 2026-06-19 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_decision_trace"
down_revision: str | Sequence[str] | None = "0004_silences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_trace",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("webhook_event_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("skip_code", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("importance", sa.String(length=20), nullable=True),
        sa.Column("is_periodic_reminder", sa.Boolean(), server_default=text("false"), nullable=False),
        sa.Column("matched_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_trace_webhook_event_id", "decision_trace", ["webhook_event_id"], unique=False)
    op.create_index("ix_decision_trace_created_at", "decision_trace", ["created_at"], unique=False)
    op.create_index("ix_decision_trace_outcome", "decision_trace", ["outcome"], unique=False)
    op.create_index("ix_decision_trace_skip_code", "decision_trace", ["skip_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_decision_trace_skip_code", table_name="decision_trace")
    op.drop_index("ix_decision_trace_outcome", table_name="decision_trace")
    op.drop_index("ix_decision_trace_created_at", table_name="decision_trace")
    op.drop_index("ix_decision_trace_webhook_event_id", table_name="decision_trace")
    op.drop_table("decision_trace")
