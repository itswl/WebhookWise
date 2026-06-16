"""consolidated schema baseline

Single baseline that creates the full current schema. It replaces the former
incremental chain (0001_current_schema -> 0002 -> ... -> 0006), squashed away
now that no live database predates it. Verified to produce a byte-identical
schema to running that whole chain.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('ai_usage_logs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('tokens_in', sa.Integer(), nullable=False),
    sa.Column('tokens_out', sa.Integer(), nullable=False),
    sa.Column('cost_estimate', sa.Float(), nullable=False),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('route_type', sa.String(length=20), nullable=True),
    sa.Column('alert_hash', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_ai_usage_logs_timestamp_route', 'ai_usage_logs', ['timestamp', 'route_type'], unique=False)
    op.create_index(op.f('ix_ai_usage_logs_alert_hash'), 'ai_usage_logs', ['alert_hash'], unique=False)
    op.create_index(op.f('ix_ai_usage_logs_timestamp'), 'ai_usage_logs', ['timestamp'], unique=False)
    op.create_table('archived_webhook_events',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=100), nullable=False),
    sa.Column('client_ip', sa.String(length=50), nullable=True),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('raw_payload', sa.LargeBinary(), nullable=True),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('parsed_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('alert_hash', sa.String(length=64), nullable=True),
    sa.Column('ai_analysis', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('importance', sa.String(length=20), nullable=True),
    sa.Column('processing_status', sa.String(length=20), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=True),
    sa.Column('next_retry_at', sa.DateTime(), nullable=True),
    sa.Column('failure_reason', sa.String(length=500), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('forward_status', sa.String(length=20), nullable=True),
    sa.Column('prev_alert_id', sa.BigInteger(), nullable=True),
    sa.Column('is_duplicate', sa.Boolean(), nullable=True),
    sa.Column('duplicate_of', sa.Integer(), nullable=True),
    sa.Column('duplicate_count', sa.Integer(), nullable=True),
    sa.Column('last_notified_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_archived_hash_timestamp', 'archived_webhook_events', ['alert_hash', 'timestamp'], unique=False)
    op.create_index(op.f('ix_archived_webhook_events_alert_hash'), 'archived_webhook_events', ['alert_hash'], unique=False)
    op.create_index(op.f('ix_archived_webhook_events_archived_at'), 'archived_webhook_events', ['archived_at'], unique=False)
    op.create_index(op.f('ix_archived_webhook_events_request_id'), 'archived_webhook_events', ['request_id'], unique=False)
    op.create_index(op.f('ix_archived_webhook_events_timestamp'), 'archived_webhook_events', ['timestamp'], unique=False)
    op.create_table('forward_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('match_event_type', sa.String(length=200), nullable=False),
    sa.Column('match_importance', sa.String(length=50), nullable=False),
    sa.Column('match_duplicate', sa.String(length=20), nullable=False),
    sa.Column('match_source', sa.String(length=200), nullable=False),
    sa.Column('match_project', sa.String(length=200), server_default='', nullable=False),
    sa.Column('match_region', sa.String(length=200), server_default='', nullable=False),
    sa.Column('match_environment', sa.String(length=200), server_default='', nullable=False),
    sa.Column('match_payload', sa.String(length=512), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=False),
    sa.Column('target_url', sa.String(length=500), nullable=False),
    sa.Column('target_name', sa.String(length=100), nullable=False),
    sa.Column('stop_on_match', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_forward_rules_priority', 'forward_rules', ['priority'], unique=False)
    op.create_table('suppressed_records',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('alert_hash', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=100), nullable=False),
    sa.Column('relation', sa.String(length=32), nullable=False),
    sa.Column('root_cause_event_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('related_alert_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_suppressed_records_created_at', 'suppressed_records', ['created_at'], unique=False)
    op.create_index('idx_suppressed_records_hash_created', 'suppressed_records', ['alert_hash', 'created_at'], unique=False)
    op.create_table('webhook_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=100), nullable=False),
    sa.Column('client_ip', sa.String(length=50), nullable=True),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('raw_payload', sa.LargeBinary(), nullable=True),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('parsed_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('alert_hash', sa.String(length=64), nullable=True),
    sa.Column('dedup_key', sa.String(length=64), nullable=True),
    sa.Column('ai_analysis', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('importance', sa.String(length=20), nullable=True),
    sa.Column('processing_status', sa.String(length=20), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('next_retry_at', sa.DateTime(), nullable=True),
    sa.Column('failure_reason', sa.String(length=500), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('forward_status', sa.String(length=20), nullable=True),
    sa.Column('prev_alert_id', sa.BigInteger(), nullable=True),
    sa.Column('is_duplicate', sa.Boolean(), nullable=False),
    sa.Column('duplicate_of', sa.Integer(), nullable=True),
    sa.Column('duplicate_count', sa.Integer(), nullable=False),
    sa.Column('last_notified_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['duplicate_of'], ['webhook_events.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['prev_alert_id'], ['webhook_events.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_dedup_key_timestamp', 'webhook_events', ['dedup_key', 'timestamp'], unique=False)
    op.create_index('idx_hash_timestamp', 'webhook_events', ['alert_hash', 'timestamp'], unique=False)
    op.create_index('idx_webhook_events_dead_letter', 'webhook_events', ['id'], unique=False, postgresql_where=sa.text("processing_status = 'dead_letter'"))
    op.create_index(op.f('ix_webhook_events_alert_hash'), 'webhook_events', ['alert_hash'], unique=False)
    op.create_index(op.f('ix_webhook_events_dedup_key'), 'webhook_events', ['dedup_key'], unique=False)
    op.create_index(op.f('ix_webhook_events_request_id'), 'webhook_events', ['request_id'], unique=True)
    op.create_index(op.f('ix_webhook_events_timestamp'), 'webhook_events', ['timestamp'], unique=False)
    op.create_table('deep_analyses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('webhook_event_id', sa.Integer(), nullable=False),
    sa.Column('engine', sa.String(length=20), nullable=False),
    sa.Column('user_question', sa.Text(), nullable=False),
    sa.Column('analysis_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('openclaw_run_id', sa.String(length=64), nullable=True),
    sa.Column('openclaw_session_key', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('poll_attempts', sa.Integer(), nullable=False),
    sa.Column('next_poll_at', sa.DateTime(), nullable=True),
    sa.Column('last_polled_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['webhook_event_id'], ['webhook_events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_deep_analyses_pending', 'deep_analyses', ['created_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('idx_deep_analyses_pending_next_poll', 'deep_analyses', ['next_poll_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index(op.f('ix_deep_analyses_openclaw_run_id'), 'deep_analyses', ['openclaw_run_id'], unique=False)
    op.create_index(op.f('ix_deep_analyses_status'), 'deep_analyses', ['status'], unique=False)
    op.create_index(op.f('ix_deep_analyses_webhook_event_id'), 'deep_analyses', ['webhook_event_id'], unique=False)
    op.create_table('forward_outboxes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('webhook_event_id', sa.Integer(), nullable=True),
    sa.Column('original_event_id', sa.Integer(), nullable=True),
    sa.Column('forward_rule_id', sa.Integer(), nullable=True),
    sa.Column('rule_name', sa.String(length=100), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=False),
    sa.Column('target_url', sa.String(length=500), nullable=False),
    sa.Column('target_name', sa.String(length=100), nullable=False),
    sa.Column('is_periodic_reminder', sa.Boolean(), nullable=False),
    sa.Column('channel_name', sa.String(length=32), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('forward_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('analysis_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('formatted_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('response_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['forward_rule_id'], ['forward_rules.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['original_event_id'], ['webhook_events.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['webhook_event_id'], ['webhook_events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('idx_forward_outboxes_pending', 'forward_outboxes', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status IN ('pending', 'retrying')"))
    op.create_index(op.f('ix_forward_outboxes_status'), 'forward_outboxes', ['status'], unique=False)
    op.create_index(op.f('ix_forward_outboxes_webhook_event_id'), 'forward_outboxes', ['webhook_event_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_forward_outboxes_webhook_event_id'), table_name='forward_outboxes')
    op.drop_index(op.f('ix_forward_outboxes_status'), table_name='forward_outboxes')
    op.drop_index('idx_forward_outboxes_pending', table_name='forward_outboxes', postgresql_where=sa.text("status IN ('pending', 'retrying')"))
    op.drop_table('forward_outboxes')
    op.drop_index(op.f('ix_deep_analyses_webhook_event_id'), table_name='deep_analyses')
    op.drop_index(op.f('ix_deep_analyses_status'), table_name='deep_analyses')
    op.drop_index(op.f('ix_deep_analyses_openclaw_run_id'), table_name='deep_analyses')
    op.drop_index('idx_deep_analyses_pending_next_poll', table_name='deep_analyses', postgresql_where=sa.text("status = 'pending'"))
    op.drop_index('idx_deep_analyses_pending', table_name='deep_analyses', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('deep_analyses')
    op.drop_index(op.f('ix_webhook_events_timestamp'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_request_id'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_dedup_key'), table_name='webhook_events')
    op.drop_index(op.f('ix_webhook_events_alert_hash'), table_name='webhook_events')
    op.drop_index('idx_webhook_events_dead_letter', table_name='webhook_events', postgresql_where=sa.text("processing_status = 'dead_letter'"))
    op.drop_index('idx_hash_timestamp', table_name='webhook_events')
    op.drop_index('idx_dedup_key_timestamp', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_index('idx_suppressed_records_hash_created', table_name='suppressed_records')
    op.drop_index('idx_suppressed_records_created_at', table_name='suppressed_records')
    op.drop_table('suppressed_records')
    op.drop_index('idx_forward_rules_priority', table_name='forward_rules')
    op.drop_table('forward_rules')
    op.drop_index(op.f('ix_archived_webhook_events_timestamp'), table_name='archived_webhook_events')
    op.drop_index(op.f('ix_archived_webhook_events_request_id'), table_name='archived_webhook_events')
    op.drop_index(op.f('ix_archived_webhook_events_archived_at'), table_name='archived_webhook_events')
    op.drop_index(op.f('ix_archived_webhook_events_alert_hash'), table_name='archived_webhook_events')
    op.drop_index('idx_archived_hash_timestamp', table_name='archived_webhook_events')
    op.drop_table('archived_webhook_events')
    op.drop_index(op.f('ix_ai_usage_logs_timestamp'), table_name='ai_usage_logs')
    op.drop_index(op.f('ix_ai_usage_logs_alert_hash'), table_name='ai_usage_logs')
    op.drop_index('idx_ai_usage_logs_timestamp_route', table_name='ai_usage_logs')
    op.drop_table('ai_usage_logs')
