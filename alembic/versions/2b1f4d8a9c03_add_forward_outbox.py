"""add transactional forwarding outbox

Revision ID: 2b1f4d8a9c03
Revises: 6a7b8c9d0e1f
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2b1f4d8a9c03"
down_revision: str | Sequence[str] | None = "6a7b8c9d0e1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forward_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("webhook_event_id", sa.Integer(), nullable=False),
        sa.Column("original_event_id", sa.Integer(), nullable=True),
        sa.Column("forward_rule_id", sa.Integer(), nullable=True),
        sa.Column("rule_name", sa.String(length=100), nullable=True),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_url", sa.String(length=500), nullable=True),
        sa.Column("target_name", sa.String(length=100), nullable=True),
        sa.Column("is_periodic_reminder", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("forward_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("analysis_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("idx_forward_outbox_event", "forward_outbox", ["webhook_event_id"])
    op.create_index(
        "idx_forward_outbox_pending",
        "forward_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'retrying')"),
    )
    op.create_index(op.f("ix_forward_outbox_status"), "forward_outbox", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_forward_outbox_status"), table_name="forward_outbox")
    op.drop_index("idx_forward_outbox_pending", table_name="forward_outbox")
    op.drop_index("idx_forward_outbox_event", table_name="forward_outbox")
    op.drop_table("forward_outbox")
