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
    from sqlalchemy import text

    op.execute("SET lock_timeout = '5s'")

    conn = op.get_bind()

    # 检查 webhook_events.raw_payload 当前类型
    result = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'webhook_events' AND column_name = 'raw_payload'"
        )
    )
    row = result.scalar()
    if row and row != "bytea":
        # TEXT -> BYTEA: 使用 convert_to 将文本正确编码为字节
        op.execute(
            "ALTER TABLE webhook_events " "ALTER COLUMN raw_payload TYPE BYTEA USING convert_to(raw_payload, 'UTF8')"
        )

    # 检查 archived_webhook_events.raw_payload 当前类型
    result = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'archived_webhook_events' AND column_name = 'raw_payload'"
        )
    )
    row = result.scalar()
    if row and row != "bytea":
        op.execute(
            "ALTER TABLE archived_webhook_events "
            "ALTER COLUMN raw_payload TYPE BYTEA USING convert_to(raw_payload, 'UTF8')"
        )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE webhook_events ALTER COLUMN raw_payload TYPE TEXT USING encode(raw_payload, 'escape')")
    op.execute(
        "ALTER TABLE archived_webhook_events ALTER COLUMN raw_payload TYPE TEXT USING encode(raw_payload, 'escape')"
    )
