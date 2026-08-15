"""Record which deep-analysis prompt asked the question

A deep analysis is triggered now and answered minutes later. By the time the
report lands the prompt may have been edited — the admin API can reload it
without a deploy — so the fingerprint has to be captured when the question is
asked, not when the answer arrives. The poller has no way to know what was sent.

Empty for existing rows: they were asked under a prompt this column cannot
recover, and pretending otherwise would be worse than saying nothing.

Revision ID: 0032_da_prompt_version
Revises: 0031_inbound_rules
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_da_prompt_version"
down_revision = "0031_inbound_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deep_analyses",
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("deep_analyses", "prompt_version")
