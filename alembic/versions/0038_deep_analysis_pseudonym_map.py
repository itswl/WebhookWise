"""deep_analyses: carry the reversible-mask map across the gateway round-trip

The trigger masks estate identifiers in the outbound prompt (anon-* tokens);
the report comes back minutes later on a different process, which needs the
same map to hand the operator a report about the real hosts. The row is the
only thing both sides reliably share, so the map rides it — written at trigger
time, consumed once by the poller's unmask, then cleared.

Revision ID: 0038_da_pseudonym_map
Revises: 0037_remediation_verification
Create Date: 2026-08-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_da_pseudonym_map"
down_revision: str | Sequence[str] | None = "0037_remediation_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deep_analyses",
        sa.Column("pseudonym_map", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deep_analyses", "pseudonym_map")
