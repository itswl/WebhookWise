from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.datetime_utils import utcnow
from db.session import Base


class KBDocument(Base):
    """A knowledge-base chunk: one slice of an internal doc + its embedding.

    A source document is split into chunks; each chunk is one row carrying its
    text, a stored embedding vector (JSONB list of floats — no pgvector, so the
    prod Postgres image is untouched; cosine similarity is computed in Python at
    retrieval time, which is fine for a small corpus), and the embedding model
    used. ``content_hash`` makes re-ingesting the same chunk idempotent.

    This is FACTUAL knowledge (what a resource is, error-code meaning, owner,
    runbook link) — deliberately distinct from the procedural Skills system.
    """

    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    # Where the chunk came from (wiki link, file name) — shown as a citation.
    source_ref: Mapped[str | None] = mapped_column(String(500))
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    # Idempotency / dedup key: sha256 of (title + chunk_index + content).
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Stored embedding (list[float]) + the model that produced it. "placeholder"
    # marks a deterministic local embedding (no external endpoint configured);
    # such rows are re-embedded once a real KB_EMBEDDING_* endpoint is set.
    embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    embedding_model: Mapped[str | None] = mapped_column(String(100), index=True)

    # Optional filter facets (e.g. {"service": "VikingDB", "project": "..."}).
    tags: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # "published" chunks feed RAG retrieval; "draft" chunks (e.g. sedimented from
    # a resolved incident, awaiting operator review) are excluded so unreviewed
    # content can never influence AI analysis. See migration 0016.
    status: Mapped[str] = mapped_column(String(20), default="published", server_default="published")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utcnow(), onupdate=lambda: utcnow())
