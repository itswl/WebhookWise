"""Take one vendor's name off the deep-analysis layer

WebhookWise talks to an external investigator over HTTP. That layer was named
after the first product to sit behind it — OpenClaw — everywhere: 26 env vars,
two DB columns, three JSON keys, a forward target type, a metric label. Then a
second dialect appeared (hermes), and a third gateway was deployed (hookprobe),
and the name became a lie the UI repeated on every card.

So the layer is now DEEP_ANALYSIS_* / gateway_*, and the product names live
where they belong: as values of DEEP_ANALYSIS_PLATFORM.

No compatibility aliases, by decision — which means the data has to move in the
same step as the code, or old rows stop rendering and one forward rule stops
matching a channel:

  * deep_analyses.openclaw_run_id      -> gateway_run_id      (+ its index)
  * deep_analyses.openclaw_session_key -> gateway_session_key
  * the _openclaw_run_id / _openclaw_session_key / _openclaw_text keys inside
    every stored analysis JSON (deep_analyses.analysis_result,
    forward_outboxes.analysis_result, webhook_events.ai_analysis)
  * target_type / channel_name value 'openclaw' -> 'deep_analysis'
    (forward_rules, forward_outboxes)

deep_analyses.engine is deliberately NOT rewritten. It records which platform
answered at the time, and 'openclaw' is what the configuration said then — that
is history, not a misnomer.

Reversible: downgrade() puts every name and value back.

Revision ID: 0029_neutral_deep_analysis
Revises: 0028_importance_overrides
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_neutral_deep_analysis"
down_revision: str | Sequence[str] | None = "0028_importance_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> JSONB column holding an analysis result
_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("deep_analyses", "analysis_result"),
    ("forward_outboxes", "analysis_result"),
    ("webhook_events", "ai_analysis"),
)

_JSON_KEYS: tuple[tuple[str, str], ...] = (
    ("_openclaw_run_id", "_gateway_run_id"),
    ("_openclaw_session_key", "_gateway_session_key"),
    ("_openclaw_text", "_gateway_text"),
)


def _rename_json_keys(pairs: tuple[tuple[str, str], ...]) -> None:
    """Rewrite top-level JSONB keys in place, touching only affected rows.

    Done as one UPDATE per key per table rather than a read-modify-write loop:
    the rewrite has to be atomic with the column rename, and pulling every
    analysis blob into Python to put it back would be slower and could partially
    apply.
    """
    for table, column in _JSON_COLUMNS:
        for old, new in pairs:
            op.execute(
                f"""
                UPDATE {table}
                   SET {column} = ({column} - '{old}') || jsonb_build_object('{new}', {column} -> '{old}')
                 WHERE {column} ? '{old}'
                """  # noqa: S608 - table/column names are literals from _JSON_COLUMNS, never input
            )


def upgrade() -> None:
    op.alter_column("deep_analyses", "openclaw_run_id", new_column_name="gateway_run_id")
    op.alter_column("deep_analyses", "openclaw_session_key", new_column_name="gateway_session_key")
    op.execute("ALTER INDEX IF EXISTS ix_deep_analyses_openclaw_run_id RENAME TO ix_deep_analyses_gateway_run_id")

    _rename_json_keys(_JSON_KEYS)

    # A rule whose target_type no longer matches any channel silently stops
    # delivering, so this value has to move with the code that reads it.
    op.execute("UPDATE forward_rules SET target_type = 'deep_analysis' WHERE target_type = 'openclaw'")
    op.execute("UPDATE forward_outboxes SET target_type = 'deep_analysis' WHERE target_type = 'openclaw'")
    op.execute("UPDATE forward_outboxes SET channel_name = 'deep_analysis' WHERE channel_name = 'openclaw'")


def downgrade() -> None:
    op.execute("UPDATE forward_outboxes SET channel_name = 'openclaw' WHERE channel_name = 'deep_analysis'")
    op.execute("UPDATE forward_outboxes SET target_type = 'openclaw' WHERE target_type = 'deep_analysis'")
    op.execute("UPDATE forward_rules SET target_type = 'openclaw' WHERE target_type = 'deep_analysis'")

    _rename_json_keys(tuple((new, old) for old, new in _JSON_KEYS))

    op.execute("ALTER INDEX IF EXISTS ix_deep_analyses_gateway_run_id RENAME TO ix_deep_analyses_openclaw_run_id")
    op.alter_column("deep_analyses", "gateway_session_key", new_column_name="openclaw_session_key")
    op.alter_column("deep_analyses", "gateway_run_id", new_column_name="openclaw_run_id")
