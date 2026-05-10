"""int to bool: is_duplicate, beyond_window

Revision ID: 5e6f7a8b9d0e
Revises: 4d5e6f7a8b9c
Create Date: 2026-05-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5e6f7a8b9d0e"
down_revision: str | Sequence[str] | None = "4d5e6f7a8b9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE webhook_events
            ALTER COLUMN is_duplicate DROP DEFAULT,
            ALTER COLUMN is_duplicate TYPE BOOLEAN USING (is_duplicate::text::boolean),
            ALTER COLUMN is_duplicate SET DEFAULT FALSE,
            ALTER COLUMN beyond_window DROP DEFAULT,
            ALTER COLUMN beyond_window TYPE BOOLEAN USING (beyond_window::text::boolean),
            ALTER COLUMN beyond_window SET DEFAULT FALSE
        """
    )
    op.execute(
        """
        ALTER TABLE archived_webhook_events
            ALTER COLUMN is_duplicate TYPE BOOLEAN USING (is_duplicate::text::boolean),
            ALTER COLUMN beyond_window TYPE BOOLEAN USING (beyond_window::text::boolean)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE webhook_events
            ALTER COLUMN is_duplicate DROP DEFAULT,
            ALTER COLUMN is_duplicate TYPE INTEGER USING (is_duplicate::int),
            ALTER COLUMN is_duplicate SET DEFAULT 0,
            ALTER COLUMN beyond_window DROP DEFAULT,
            ALTER COLUMN beyond_window TYPE INTEGER USING (beyond_window::int),
            ALTER COLUMN beyond_window SET DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE archived_webhook_events
            ALTER COLUMN is_duplicate TYPE INTEGER USING (CASE WHEN is_duplicate THEN 1 ELSE 0 END),
            ALTER COLUMN beyond_window TYPE INTEGER USING (CASE WHEN beyond_window THEN 1 ELSE 0 END)
        """
    )
