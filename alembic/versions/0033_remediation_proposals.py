"""remediation_proposals: let something suggest an action a person still has to allow

The Action Center runs a small, audited set of commands when an operator clicks
one. An agent looking at the same dead letters, the same stuck outbox, the same
unacknowledged incident can see the same thing needs doing and has no way to say
so — so either a human relays it by hand, or the agent gets execute rights,
which is the wrong trade to make for convenience.

A proposal is inert. It records the action, its arguments, who suggested it and
why, and it expires. Approving one executes through exactly the same
`run_remediation` path the button uses, so the set of things that can actually
happen to this deployment does not grow.

The partial unique index is the interesting constraint: one PENDING proposal per
action+resource. An agent in a retry loop must not be able to fill a human's
review queue with the same suggestion two hundred times.

Revision ID: 0033_remediation_proposals
Revises: 0032_da_prompt_version
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_remediation_proposals"
down_revision: str | Sequence[str] | None = "0032_da_prompt_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remediation_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("batch_size", sa.Integer(), server_default="50", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(length=100), server_default="agent", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remediation_proposals_status_created",
        "remediation_proposals",
        ["status", "created_at"],
    )
    # Partial: only pending rows take part, so a decided proposal never blocks a
    # later suggestion of the same thing.
    op.create_index(
        "ix_remediation_proposals_pending_unique",
        "remediation_proposals",
        ["action", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_remediation_proposals_pending_unique", table_name="remediation_proposals")
    op.drop_index("ix_remediation_proposals_status_created", table_name="remediation_proposals")
    op.drop_table("remediation_proposals")
