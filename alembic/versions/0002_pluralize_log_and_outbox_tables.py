"""pluralize log and outbox table names

Revision ID: 0002_pluralize_log_and_outbox_tables
Revises: 0001_current_schema
Create Date: 2026-05-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_pluralize_log_and_outbox_tables"
down_revision: str | Sequence[str] | None = "0001_current_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _rename_index_if_postgres(old_name: str, new_name: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(f'ALTER INDEX IF EXISTS "{old_name}" RENAME TO "{new_name}"'))


def _rename_sequence_if_postgres(old_name: str, new_name: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(f'ALTER SEQUENCE IF EXISTS "{old_name}" RENAME TO "{new_name}"'))


def _rename_constraint_if_postgres(table_name: str, old_name: str, new_name: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{old_name}' "
            f"AND conrelid = '{table_name}'::regclass) THEN "
            f'ALTER TABLE "{table_name}" RENAME CONSTRAINT "{old_name}" TO "{new_name}"; '
            "END IF; END $$;"
        )
    )


def upgrade() -> None:
    """Upgrade schema."""
    if _has_table("ai_usage_log") and not _has_table("ai_usage_logs"):
        op.rename_table("ai_usage_log", "ai_usage_logs")
        _rename_sequence_if_postgres("ai_usage_log_id_seq", "ai_usage_logs_id_seq")
        _rename_constraint_if_postgres("ai_usage_logs", "ai_usage_log_pkey", "ai_usage_logs_pkey")
        _rename_index_if_postgres("ix_ai_usage_log_timestamp", "ix_ai_usage_logs_timestamp")
        _rename_index_if_postgres("ix_ai_usage_log_alert_hash", "ix_ai_usage_logs_alert_hash")
        _rename_index_if_postgres("idx_usage_timestamp_route", "idx_ai_usage_logs_timestamp_route")

    if _has_table("forward_outbox") and not _has_table("forward_outboxes"):
        op.rename_table("forward_outbox", "forward_outboxes")
        _rename_sequence_if_postgres("forward_outbox_id_seq", "forward_outboxes_id_seq")
        _rename_constraint_if_postgres("forward_outboxes", "forward_outbox_pkey", "forward_outboxes_pkey")
        _rename_constraint_if_postgres(
            "forward_outboxes", "forward_outbox_idempotency_key_key", "forward_outboxes_idempotency_key_key"
        )
        _rename_constraint_if_postgres(
            "forward_outboxes", "forward_outbox_webhook_event_id_fkey", "forward_outboxes_webhook_event_id_fkey"
        )
        _rename_constraint_if_postgres(
            "forward_outboxes", "forward_outbox_original_event_id_fkey", "forward_outboxes_original_event_id_fkey"
        )
        _rename_constraint_if_postgres(
            "forward_outboxes", "forward_outbox_forward_rule_id_fkey", "forward_outboxes_forward_rule_id_fkey"
        )
        _rename_index_if_postgres("ix_forward_outbox_status", "ix_forward_outboxes_status")
        _rename_index_if_postgres("ix_forward_outbox_webhook_event_id", "ix_forward_outboxes_webhook_event_id")
        _rename_index_if_postgres("idx_forward_outbox_pending", "idx_forward_outboxes_pending")
        _rename_index_if_postgres("idx_forward_outbox_event", "idx_forward_outboxes_event")


def downgrade() -> None:
    """Downgrade schema."""
    if _has_table("ai_usage_logs") and not _has_table("ai_usage_log"):
        op.rename_table("ai_usage_logs", "ai_usage_log")
        _rename_sequence_if_postgres("ai_usage_logs_id_seq", "ai_usage_log_id_seq")
        _rename_constraint_if_postgres("ai_usage_log", "ai_usage_logs_pkey", "ai_usage_log_pkey")
        _rename_index_if_postgres("ix_ai_usage_logs_timestamp", "ix_ai_usage_log_timestamp")
        _rename_index_if_postgres("ix_ai_usage_logs_alert_hash", "ix_ai_usage_log_alert_hash")
        _rename_index_if_postgres("idx_ai_usage_logs_timestamp_route", "idx_usage_timestamp_route")

    if _has_table("forward_outboxes") and not _has_table("forward_outbox"):
        op.rename_table("forward_outboxes", "forward_outbox")
        _rename_sequence_if_postgres("forward_outboxes_id_seq", "forward_outbox_id_seq")
        _rename_constraint_if_postgres("forward_outbox", "forward_outboxes_pkey", "forward_outbox_pkey")
        _rename_constraint_if_postgres(
            "forward_outbox", "forward_outboxes_idempotency_key_key", "forward_outbox_idempotency_key_key"
        )
        _rename_constraint_if_postgres(
            "forward_outbox", "forward_outboxes_webhook_event_id_fkey", "forward_outbox_webhook_event_id_fkey"
        )
        _rename_constraint_if_postgres(
            "forward_outbox", "forward_outboxes_original_event_id_fkey", "forward_outbox_original_event_id_fkey"
        )
        _rename_constraint_if_postgres(
            "forward_outbox", "forward_outboxes_forward_rule_id_fkey", "forward_outbox_forward_rule_id_fkey"
        )
        _rename_index_if_postgres("ix_forward_outboxes_status", "ix_forward_outbox_status")
        _rename_index_if_postgres("ix_forward_outboxes_webhook_event_id", "ix_forward_outbox_webhook_event_id")
        _rename_index_if_postgres("idx_forward_outboxes_pending", "idx_forward_outbox_pending")
        _rename_index_if_postgres("idx_forward_outboxes_event", "idx_forward_outbox_event")
