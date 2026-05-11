"""Add retry due state and reference integrity.

Revision ID: 7f8a9b0c1d2e
Revises: 5e6f7a8b9d0e
Create Date: 2026-05-11 11:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7f8a9b0c1d2e"
down_revision: str | Sequence[str] | None = "5e6f7a8b9d0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("webhook_events") as batch_op:
        batch_op.add_column(sa.Column("next_retry_at", sa.DateTime(), nullable=True))

    op.create_index(
        "idx_retry_due",
        "webhook_events",
        ["next_retry_at"],
        postgresql_where=sa.text("processing_status = 'retry'"),
    )

    if dialect == "postgresql":
        op.execute(
            """
            update webhook_events child
            set duplicate_of = null
            where duplicate_of is not null
              and not exists (select 1 from webhook_events parent where parent.id = child.duplicate_of)
            """
        )
        op.execute(
            """
            update webhook_events child
            set prev_alert_id = null
            where prev_alert_id is not null
              and not exists (select 1 from webhook_events parent where parent.id = child.prev_alert_id)
            """
        )
        op.execute(
            "delete from forward_outbox fo where not exists "
            "(select 1 from webhook_events e where e.id = fo.webhook_event_id)"
        )
        op.execute(
            "delete from failed_forwards ff where not exists "
            "(select 1 from webhook_events e where e.id = ff.webhook_event_id)"
        )
        op.execute(
            "delete from deep_analyses da where not exists "
            "(select 1 from webhook_events e where e.id = da.webhook_event_id)"
        )
        op.execute(
            """
            update forward_outbox fo
            set original_event_id = null
            where original_event_id is not null
              and not exists (select 1 from webhook_events e where e.id = fo.original_event_id)
            """
        )
        op.execute(
            """
            update forward_outbox fo
            set forward_rule_id = null
            where forward_rule_id is not null
              and not exists (select 1 from forward_rules r where r.id = fo.forward_rule_id)
            """
        )
        op.execute(
            """
            update failed_forwards ff
            set forward_rule_id = null
            where forward_rule_id is not null
              and not exists (select 1 from forward_rules r where r.id = ff.forward_rule_id)
            """
        )

    op.create_foreign_key(
        "fk_webhook_events_duplicate_of",
        "webhook_events",
        "webhook_events",
        ["duplicate_of"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_webhook_events_prev_alert_id",
        "webhook_events",
        "webhook_events",
        ["prev_alert_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_forward_outbox_webhook_event_id",
        "forward_outbox",
        "webhook_events",
        ["webhook_event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_forward_outbox_original_event_id",
        "forward_outbox",
        "webhook_events",
        ["original_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_forward_outbox_forward_rule_id",
        "forward_outbox",
        "forward_rules",
        ["forward_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_failed_forwards_webhook_event_id",
        "failed_forwards",
        "webhook_events",
        ["webhook_event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_failed_forwards_forward_rule_id",
        "failed_forwards",
        "forward_rules",
        ["forward_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_deep_analyses_webhook_event_id",
        "deep_analyses",
        "webhook_events",
        ["webhook_event_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_deep_analyses_webhook_event_id", "deep_analyses", type_="foreignkey")
    op.drop_constraint("fk_failed_forwards_forward_rule_id", "failed_forwards", type_="foreignkey")
    op.drop_constraint("fk_failed_forwards_webhook_event_id", "failed_forwards", type_="foreignkey")
    op.drop_constraint("fk_forward_outbox_forward_rule_id", "forward_outbox", type_="foreignkey")
    op.drop_constraint("fk_forward_outbox_original_event_id", "forward_outbox", type_="foreignkey")
    op.drop_constraint("fk_forward_outbox_webhook_event_id", "forward_outbox", type_="foreignkey")
    op.drop_constraint("fk_webhook_events_prev_alert_id", "webhook_events", type_="foreignkey")
    op.drop_constraint("fk_webhook_events_duplicate_of", "webhook_events", type_="foreignkey")
    op.drop_index("idx_retry_due", table_name="webhook_events")
    with op.batch_alter_table("webhook_events") as batch_op:
        batch_op.drop_column("next_retry_at")
