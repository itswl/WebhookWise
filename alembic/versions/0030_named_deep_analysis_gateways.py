"""Let different rules reach different deep-analysis gateways

Until now there was exactly one gateway, global configuration, so every
deep-analysis rule went to the same place. Supporting several means the choice
has to travel the whole hop, and it has to travel as a SNAPSHOT rather than a
lookup, for two different reasons:

  * forward_outboxes.target_gateway — the outbox is a queue. Editing a rule
    while a delivery is queued must not redirect that delivery; every other
    target field is already snapshotted here for the same reason.
  * deep_analyses.gateway_name — the poller asks a gateway for a result by
    session key. A run submitted to gateway A must be COLLECTED from gateway A;
    resolving the rule again at poll time would send the question to whatever
    the rule says now, and a session key from one gateway means nothing to
    another. This is the column that makes multi-gateway polling correct.

forward_rules.target_gateway is the configuration itself. Empty means the
gateway named "default", i.e. the flat DEEP_ANALYSIS_* settings — so every
existing rule keeps its current behaviour with no backfill.

Revision ID: 0030_named_gateways
Revises: 0029_neutral_deep_analysis
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_named_gateways"
down_revision: str | Sequence[str] | None = "0029_neutral_deep_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "forward_rules",
        sa.Column("target_gateway", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "forward_outboxes",
        sa.Column("target_gateway", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column(
        "deep_analyses",
        sa.Column("gateway_name", sa.String(length=50), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("deep_analyses", "gateway_name")
    op.drop_column("forward_outboxes", "target_gateway")
    op.drop_column("forward_rules", "target_gateway")
