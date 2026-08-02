"""runtime_settings: DB-backed overrides for operator-policy config keys

Pays down the dual-config-plane debt: the ~26 keys tagged [runtime-policy] in
.env.example.all (flapping, auto-SLA, backpressure, noise weights, notify
cadence, KB cards, trace retention) become live-editable overrides. The table
stores OVERRIDES only — an absent row means "use the env value / default", so
env remains the bootstrap plane and this stays sparse.

Revision ID: 0024_runtime_settings
Revises: 0023_mw_occurrence_identity
Create Date: 2026-08-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_runtime_settings"
down_revision: str | Sequence[str] | None = "0023_mw_occurrence_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
