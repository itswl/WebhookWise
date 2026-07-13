"""Tests for the RAG knowledge base: embedding, chunking, ingest, retrieval."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from services.kb.embedding import PLACEHOLDER_MODEL, embed_texts, is_placeholder_active
from services.kb.retrieval import RetrievedChunk, _cosine, format_context, retrieve, retrieve_context
from services.kb.store import chunk_text, count_documents, ingest_document


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ── embedding (placeholder backend) ──────────────────────────────────


@pytest.mark.asyncio
async def test_placeholder_embedding_is_deterministic_and_normalized(temp_config) -> None:
    temp_config.kb.KB_VECTOR_DIM = 64
    assert is_placeholder_active() is True  # no endpoint configured in tests

    v1, model = await embed_texts(["GPU saturation runbook"])
    v2, _ = await embed_texts(["GPU saturation runbook"])
    assert model == PLACEHOLDER_MODEL
    assert v1 == v2  # deterministic across calls
    assert len(v1[0]) == 64
    norm = sum(x * x for x in v1[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6  # L2-normalized

    # Different text → different (and not perfectly aligned) vector.
    other, _ = await embed_texts(["MongoDB connection pool"])
    assert _cosine(v1[0], other[0]) < 0.99


@pytest.mark.asyncio
async def test_embed_empty_returns_empty(temp_config) -> None:
    vecs, _ = await embed_texts([])
    assert vecs == []


@pytest.mark.asyncio
async def test_embed_uses_real_endpoint_when_configured(temp_config, monkeypatch) -> None:
    temp_config.kb.KB_EMBEDDING_API_URL = "https://embeddings.example.com/v1"
    temp_config.kb.KB_EMBEDDING_MODEL = "text-embedding-3-small"
    assert is_placeholder_active() is False

    async def fake_endpoint(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr("services.kb.embedding._embed_via_endpoint", fake_endpoint)
    vecs, model = await embed_texts(["a", "b"])
    assert model == "text-embedding-3-small"
    assert vecs == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


@pytest.mark.asyncio
async def test_embed_falls_back_to_placeholder_on_endpoint_error(temp_config, monkeypatch) -> None:
    temp_config.kb.KB_EMBEDDING_API_URL = "https://embeddings.example.com/v1"
    temp_config.kb.KB_VECTOR_DIM = 32

    async def boom(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("endpoint down")

    monkeypatch.setattr("services.kb.embedding._embed_via_endpoint", boom)
    vecs, model = await embed_texts(["x"])
    # Degrades to placeholder rather than raising — embedding is never a gate.
    assert model == PLACEHOLDER_MODEL
    assert len(vecs[0]) == 32


# ── chunking ─────────────────────────────────────────────────────────


def test_chunk_text_splits_on_paragraphs_under_limit() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = chunk_text(text, max_chars=1000)
    assert len(chunks) == 1  # all fit in one chunk
    assert "Para one." in chunks[0] and "Para three." in chunks[0]


def test_chunk_text_hard_splits_oversized_paragraph() -> None:
    chunks = chunk_text("x" * 250, max_chars=100)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)


def test_chunk_text_empty() -> None:
    assert chunk_text("   ", max_chars=100) == []


# ── ingest + retrieve (SQLite, placeholder embeddings) ───────────────


@pytest.mark.asyncio
async def test_ingest_is_idempotent_by_content_hash(
    session_factory: async_sessionmaker[AsyncSession], temp_config
) -> None:
    temp_config.kb.KB_VECTOR_DIM = 64
    async with session_factory() as session:
        r1 = await ingest_document(session, title="GPU Runbook", content="GPU usage over 90%. Scale up.")
        await session.commit()
    async with session_factory() as session:
        r2 = await ingest_document(session, title="GPU Runbook", content="GPU usage over 90%. Scale up.")
        await session.commit()
    async with session_factory() as session:
        total = await count_documents(session)

    assert r1.chunks == r2.chunks
    assert total == r1.chunks  # re-ingest upserted, did not duplicate


@pytest.mark.asyncio
async def test_retrieve_ranks_by_similarity_and_respects_enabled(
    session_factory: async_sessionmaker[AsyncSession], temp_config
) -> None:
    temp_config.kb.KB_VECTOR_DIM = 64
    temp_config.kb.KB_TOP_K = 2
    temp_config.kb.KB_MIN_SCORE = 0.0  # placeholder vectors are near-orthogonal
    async with session_factory() as session:
        await ingest_document(session, title="GPU Runbook", content="GPU saturation handling")
        await ingest_document(session, title="Mongo Runbook", content="MongoDB connection pool tuning")
        await session.commit()

    # Disabled → no retrieval regardless of corpus.
    temp_config.kb.KB_ENABLED = False
    async with session_factory() as session:
        assert await retrieve(session, "GPU saturation handling") == []

    # Enabled → the identical-text doc ranks first (placeholder matches itself).
    temp_config.kb.KB_ENABLED = True
    async with session_factory() as session:
        hits = await retrieve(session, "GPU saturation handling")
    assert hits
    assert hits[0].title == "GPU Runbook"
    assert hits[0].score > 0.99  # query == stored chunk text → ~1.0 cosine
    assert len(hits) <= 2


@pytest.mark.asyncio
async def test_retrieve_context_empty_when_disabled(
    session_factory: async_sessionmaker[AsyncSession], temp_config
) -> None:
    temp_config.kb.KB_ENABLED = False
    async with session_factory() as session:
        assert await retrieve_context(session, "anything") == ""


# ── context formatting ───────────────────────────────────────────────


def test_format_context_renders_citation_and_bounds_length() -> None:
    chunks = [
        RetrievedChunk(title="GPU", content="scale up", source_ref="wiki/gpu", score=0.9),
        RetrievedChunk(title="Mongo", content="x" * 5000, source_ref=None, score=0.5),
    ]
    out = format_context(chunks, max_chars=100)
    assert "GPU" in out
    assert "（来源：wiki/gpu）" in out
    # The 5000-char second chunk is dropped by the bound (first chunk already used).
    assert len(out) < 200


def test_format_context_empty() -> None:
    assert format_context([], max_chars=100) == ""
