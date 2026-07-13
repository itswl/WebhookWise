"""Add durable noise-reduction action history.

Revision ID: 0013_noise_reduction_actions
Revises: 0012_operator_workflow
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_noise_reduction_actions"
down_revision: str | Sequence[str] | None = "0012_operator_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "noise_reduction_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("suggestion_id", sa.String(length=160), nullable=False),
        sa.Column("action_type", sa.String(length=40), nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column(
            "before_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "after_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("estimated_notifications", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_noise_reduction_actions_suggestion_id",
        "noise_reduction_actions",
        ["suggestion_id"],
    )
    op.create_index(
        "ix_noise_reduction_actions_status_created",
        "noise_reduction_actions",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_noise_reduction_actions_status_created",
        table_name="noise_reduction_actions",
    )
    op.drop_index(
        "ix_noise_reduction_actions_suggestion_id",
        table_name="noise_reduction_actions",
    )
    op.drop_table("noise_reduction_actions")
