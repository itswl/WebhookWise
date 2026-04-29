"""raw_payload Text to BYTEA with gzip compression.

Revision ID: a3f7e2b1c9d4
Revises: 582744d1c390
Create Date: 2026-04-29
"""

from alembic import op

revision = "a3f7e2b1c9d4"
down_revision = "582744d1c390"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    # WebhookEvent 表
    op.execute("ALTER TABLE webhook_events ALTER COLUMN raw_payload TYPE BYTEA USING raw_payload::bytea")
    # ArchivedWebhookEvent 表
    op.execute("ALTER TABLE archived_webhook_events ALTER COLUMN raw_payload TYPE BYTEA USING raw_payload::bytea")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE webhook_events ALTER COLUMN raw_payload TYPE TEXT USING encode(raw_payload, 'escape')")
    op.execute(
        "ALTER TABLE archived_webhook_events ALTER COLUMN raw_payload TYPE TEXT USING encode(raw_payload, 'escape')"
    )
