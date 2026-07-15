"""add status to kb_documents for the draft/published review workflow

Resolved incidents are sedimented into the KB as ``draft`` rows; an operator
reviews and publishes them. Only ``published`` rows feed RAG retrieval, so an
unreviewed draft can never influence AI analysis. Existing rows and the seed
corpus default to ``published`` (server_default) so nothing already ingested is
hidden by this migration.

Revision ID: 0016_kb_document_status
Revises: 0015_index_diet_and_debris
Create Date: 2026-07-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_kb_document_status"
down_revision: str | Sequence[str] | None = "0015_index_diet_and_debris"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_documents",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="published"),
    )
    # Retrieval filters on status; a partial index keeps the published-only scan
    # cheap without indexing the (smaller, transient) draft set.
    op.create_index(
        "ix_kb_documents_published",
        "kb_documents",
        ["embedding_model"],
        postgresql_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index("ix_kb_documents_published", table_name="kb_documents")
    op.drop_column("kb_documents", "status")
