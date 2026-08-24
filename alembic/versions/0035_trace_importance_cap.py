"""decision_trace.importance_capped_from: make the ceiling countable

The decision-quality panel reports `override_rate` as "how often a deterministic
rule had to correct the AI's importance", and a per-rule severity CEILING is
exactly that — but it was invisible on two counts. `_has_importance_override`
looks for `_importance_override`, which promotes, while the cap writes
`_importance_cap`, which demotes; and the query filters `route == 'ai'`, while
the cap deliberately lives one layer up so it also covers `reuse` and `rechain`.
Production, one week: 89 rows capped, 81 of them on a non-ai route, panel said 0.

The column stores the judgement that was REPLACED rather than a boolean, because
the direction is the whole point: `importance_capped_from` → `importance` reads
as "the model said high, the ceiling made it medium". A boolean would have
averaged promotions and demotions into one number that means neither.

The revision id is clipped, not descriptive-as-possible: alembic_version.version_num
is VARCHAR(32), and `0035_decision_trace_importance_cap` is 34 — long enough to pass
every sqlite-backed test and then fail the real-Postgres upgrade on its final
version-row UPDATE. There is a gate for exactly this; it caught this file.

Revision ID: 0035_trace_importance_cap
Revises: 0034_inbound_action_value
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_trace_importance_cap"
down_revision: str | None = "0034_inbound_action_value"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decision_trace", sa.Column("importance_capped_from", sa.String(length=20), nullable=True))
    # Partial: capped rows are the minority (19% of a week), and the queries all
    # ask "which rows were capped", never "which were not".
    op.create_index(
        "ix_decision_trace_importance_capped_from",
        "decision_trace",
        ["importance_capped_from"],
        postgresql_where=sa.text("importance_capped_from IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_decision_trace_importance_capped_from", table_name="decision_trace")
    op.drop_column("decision_trace", "importance_capped_from")
