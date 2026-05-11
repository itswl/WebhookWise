"""int to bool: is_duplicate, beyond_window

Revision ID: 5e6f7a8b9d0e
Revises: 4d5e6f7a8b9c
Create Date: 2026-05-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5e6f7a8b9d0e"
down_revision: str | Sequence[str] | None = "4d5e6f7a8b9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOL_COLUMNS = ("is_duplicate", "beyond_window")
_TABLES = ("webhook_events", "archived_webhook_events")


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_name(*parts: str) -> str:
    return ".".join(_quote_ident(part) for part in parts)


def _normalized_sql(definition: str) -> str:
    return "".join(definition.lower().split())


def _uses_integer_bool_predicate(definition: str) -> bool:
    normalized = _normalized_sql(definition)
    return any(
        f"{column}=0" in normalized
        or f"{column}=1" in normalized
        or f"{column}in(0,1)" in normalized
        or f"{column}=any(array[0,1])" in normalized
        or f"{column}=any(array[1,0])" in normalized
        for column in _BOOL_COLUMNS
    )


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = :table
                """
            ),
            {"table": table},
        ).scalar()
    )


def _column_type(table: str, column: str) -> str | None:
    conn = op.get_bind()
    return conn.execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    ).scalar()


def _drop_integer_bool_artifacts(table: str) -> None:
    """Drop old indexes/checks that compare former int flags with 0/1."""
    if not _table_exists(table):
        return

    conn = op.get_bind()
    indexes = conn.execute(
        sa.text(
            """
            SELECT schemaname, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = :table
            """
        ),
        {"table": table},
    ).mappings()
    for index in indexes:
        if _uses_integer_bool_predicate(str(index["indexdef"])):
            op.execute(f"DROP INDEX IF EXISTS {_qualified_name(str(index['schemaname']), str(index['indexname']))}")

    constraints = conn.execute(
        sa.text(
            """
            SELECT conname, pg_get_constraintdef(c.oid) AS constraintdef
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema()
              AND t.relname = :table
              AND c.contype = 'c'
            """
        ),
        {"table": table},
    ).mappings()
    for constraint in constraints:
        if _uses_integer_bool_predicate(str(constraint["constraintdef"])):
            op.execute(
                f"ALTER TABLE {_quote_ident(table)} "
                f"DROP CONSTRAINT IF EXISTS {_quote_ident(str(constraint['conname']))}"
            )


def _ensure_boolean_column(table: str, column: str, *, default_false: bool) -> None:
    if not _table_exists(table) or _column_type(table, column) is None:
        return

    table_name = _quote_ident(table)
    column_name = _quote_ident(column)
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} DROP DEFAULT")

    if _column_type(table, column) != "boolean":
        op.execute(
            f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE BOOLEAN USING ({column_name}::text::boolean)"
        )

    if default_false:
        op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT FALSE")


def _ensure_integer_column(table: str, column: str, *, default_zero: bool) -> None:
    if not _table_exists(table) or _column_type(table, column) is None:
        return

    table_name = _quote_ident(table)
    column_name = _quote_ident(column)
    op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} DROP DEFAULT")

    if _column_type(table, column) != "integer":
        op.execute(
            f"ALTER TABLE {table_name} "
            f"ALTER COLUMN {column_name} TYPE INTEGER "
            f"USING (CASE WHEN {column_name} THEN 1 ELSE 0 END)"
        )

    if default_zero:
        op.execute(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT 0")


def upgrade() -> None:
    # Drop partial indexes/checks first: historical scripts used predicates like
    # "is_duplicate = 0", which cannot be rebuilt once the column becomes boolean.
    for table in _TABLES:
        _drop_integer_bool_artifacts(table)

    for column in _BOOL_COLUMNS:
        _ensure_boolean_column("webhook_events", column, default_false=True)
        _ensure_boolean_column("archived_webhook_events", column, default_false=False)

    # Recreate with boolean-compatible condition
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
        ON webhook_events (alert_hash)
        WHERE NOT is_duplicate
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_unique_alert_hash_original")

    for column in _BOOL_COLUMNS:
        _ensure_integer_column("webhook_events", column, default_zero=True)
        _ensure_integer_column("archived_webhook_events", column, default_zero=False)

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original
        ON webhook_events (alert_hash)
        WHERE is_duplicate = 0
        """
    )
