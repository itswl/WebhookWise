"""add kb_documents table (RAG knowledge base chunks)

One row per chunk of an internal doc: text + a JSONB-stored embedding vector
(no pgvector — cosine is computed in Python at retrieval time, so the prod
Postgres image is untouched). content_hash is unique for idempotent re-ingest.

Revision ID: 0007_kb_documents
Revises: 0006_decision_trace_ai_quality
Create Date: 2026-06-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_kb_documents"
down_revision: str | Sequence[str] | None = "0006_decision_trace_ai_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_documents_content_hash", "kb_documents", ["content_hash"], unique=True)
    op.create_index("ix_kb_documents_embedding_model", "kb_documents", ["embedding_model"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_kb_documents_embedding_model", table_name="kb_documents")
    op.drop_index("ix_kb_documents_content_hash", table_name="kb_documents")
    op.drop_table("kb_documents")
