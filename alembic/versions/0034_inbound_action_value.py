"""inbound_rules.action_value: let an inbound action carry a target

Measured over 80 production investigations, WebhookWise files 90% of alerts
`high` and the investigator that actually looked agrees on a quarter. The two
worst offenders are business-signal rules whose money keywords force `high` in
`analyze_with_rules` — a promotion that was right when it was written and is now
the main source of severity inflation.

It cannot be fixed by editing the keyword list, because the two rules need
different answers: over 25 reports the deposit alert is medium (high 5, medium
12, low 8) while the withdrawal alert is genuinely high a third of the time and
must keep waking someone. So the fix has to be per alert rule.

`importance_overrides` already exists for this and cannot help either: it keys
on alert_hash, and these alerts carry the user id in their identity — 25
distinct hashes across 25 reports. An override keyed there generalises to
nothing.

inbound_rules is the right grain (it matches on rule name, is cached with
cross-worker invalidation, and its whole purpose is policy about a named alert
rule) but its `action` is a bare verb with nowhere to put "cap it at what". This
adds that column. Nullable-free with a server default so every existing row
keeps its exact meaning.

Revision ID: 0034_inbound_action_value
Revises: 0033_remediation_proposals
Create Date: 2026-08-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_inbound_action_value"
down_revision: str | Sequence[str] | None = "0033_remediation_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbound_rules",
        sa.Column("action_value", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("inbound_rules", "action_value")
