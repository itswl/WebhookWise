"""Backfill decision_trace.importance_capped_from from the analysis that recorded it

0035 made the ceiling countable going forward and left the panel reading 0 for
everything already capped — which looks exactly like the bug it just fixed. The
judgement the ceiling replaced was never lost: `apply_importance_cap` has written
`ai_analysis._importance_cap = {"judged": ..., "capped_to": ..., "rule": ...}` on
the event since the cap shipped, precisely so a capped severity stays
distinguishable from a judged one.

So this reads it back. Idempotent by `IS NULL`, and derived rather than guessed:
a row with no marker stays NULL, because NULL has to keep meaning "no ceiling
fired" for the partial index and every count above it.

Measured on production at the time of writing: 89 rows, all inside one week,
which is the whole history — the cap itself is that new.

Revision ID: 0036_backfill_capped_from
Revises: 0035_trace_importance_cap
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036_backfill_capped_from"
down_revision: str | None = "0035_trace_importance_cap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Left as one UPDATE rather than a batched loop: the source is a JSONB key on
    # a table this deployment holds thousands of rows in, not millions, and a
    # migration that needs a progress bar is a migration that should be a script.
    op.execute(
        """
        UPDATE decision_trace AS dt
           SET importance_capped_from = left(we.ai_analysis -> '_importance_cap' ->> 'judged', 20)
          FROM webhook_events AS we
         WHERE we.id = dt.webhook_event_id
           AND dt.importance_capped_from IS NULL
           AND we.ai_analysis ? '_importance_cap'
           AND coalesce(we.ai_analysis -> '_importance_cap' ->> 'judged', '') <> ''
        """
    )


def downgrade() -> None:
    # Nothing to undo: 0035's downgrade drops the column, and clearing values
    # here would destroy any ruling filed after this ran.
    pass
