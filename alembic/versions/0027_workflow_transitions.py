"""workflow_transitions: make 接手 / 标记解决 reversible

The audit log already records what a workflow change BECAME, in prose. That is
enough to read the history and not enough to reverse it: undoing needs the
prior values structurally, and it needs to know whether anything has happened
to the resource since. A misclick on Acknowledge or Resolve — easiest of all
from a Feishu card on a phone — was previously only fixable by remembering
what the state used to be and setting it back by hand.

Same shape as noise_reduction_actions, deliberately: that table already
encodes the safety rule this needs, which is that an undo applies only while
the resource still matches `after_state`. Taking back your own misclick must
never quietly discard somebody else's later decision.

Revision ID: 0027_workflow_transitions
Revises: 0026_trace_alert_name_refill
Create Date: 2026-08-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027_workflow_transitions"
down_revision: str | Sequence[str] | None = "0026_trace_alert_name_refill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_transitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="applied", nullable=False),
        sa.Column("actor", sa.String(length=100), server_default="dashboard", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The undo path always asks the same question: what was the most recent
    # still-applied change to THIS resource?
    op.create_index(
        "ix_workflow_transitions_resource",
        "workflow_transitions",
        ["resource_type", "resource_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_transitions_resource", table_name="workflow_transitions")
    op.drop_table("workflow_transitions")
