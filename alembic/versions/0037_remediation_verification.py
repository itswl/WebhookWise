"""remediation_proposals: the world gets to disagree with the executor

Absorbed from the CISRE stance that API success, model claims, or a previous
health check do not equal fixed infrastructure. `status`/`result` already
separate "did a person allow it" from "did the call work"; these columns hold
the third question — did the TARGET's own state confirm the fix held when a
worker read it back a few minutes later. `verify_status` is scheduled ->
verified / unrecovered / unverifiable; `verify_detail` carries what the
readback actually saw (counts, sample ids, observed statuses), because a
verdict nobody can interrogate is a verdict nobody will trust.

Revision ID: 0037_remediation_verification
Revises: 0036_backfill_capped_from
Create Date: 2026-08-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_remediation_verification"
down_revision: str | Sequence[str] | None = "0036_backfill_capped_from"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "remediation_proposals",
        sa.Column("verify_status", sa.String(length=20), nullable=False, server_default=""),
    )
    op.add_column(
        "remediation_proposals",
        sa.Column("verify_detail", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("remediation_proposals", sa.Column("verified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("remediation_proposals", "verified_at")
    op.drop_column("remediation_proposals", "verify_detail")
    op.drop_column("remediation_proposals", "verify_status")
