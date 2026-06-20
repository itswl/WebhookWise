"""Knowledge-base retrieval: embed the alert, cosine-rank chunks, format context.

For a small corpus (tens–hundreds of chunks) the stored embeddings are loaded
and ranked in Python — no pgvector, no extra component. When the corpus grows,
swap the candidate fetch + scoring for a vector index behind this same function.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.logger import get_logger
from models import KBDocument
from services.kb.embedding import embed_texts

logger = get_logger("kb.retrieval")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    title: str
    content: str
    source_ref: str | None
    score: float


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def retrieve(session: AsyncSession, query_text: str) -> list[RetrievedChunk]:
    """Return the top-K knowledge chunks most similar to query_text.

    Returns [] when KB is disabled, the query is empty, or nothing clears the
    similarity floor — callers treat empty as "no context to inject".
    """
    cfg = get_config_manager().kb
    if not cfg.KB_ENABLED or not query_text.strip():
        return []

    rows = (await session.execute(select(KBDocument).where(KBDocument.embedding.isnot(None)))).scalars().all()
    if not rows:
        return []

    query_vecs, _model = await embed_texts([query_text])
    if not query_vecs:
        return []
    query_vec = query_vecs[0]

    scored: list[RetrievedChunk] = []
    for row in rows:
        score = _cosine(query_vec, row.embedding or [])
        if score >= cfg.KB_MIN_SCORE:
            scored.append(
                RetrievedChunk(title=row.title, content=row.content, source_ref=row.source_ref, score=score)
            )
    scored.sort(key=lambda c: c.score, reverse=True)
    top = scored[: max(1, cfg.KB_TOP_K)]
    if top:
        logger.debug("[KB] Retrieved %d chunks (top score %.3f)", len(top), top[0].score)
    return top


def format_context(chunks: list[RetrievedChunk], max_chars: int) -> str:
    """Render retrieved chunks into a prompt block (empty string if none).

    Output is bounded to max_chars so a large corpus can't blow up the prompt.
    Kept in Chinese to match the analysis prompt (product decision).
    """
    if not chunks:
        return ""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        cite = f"（来源：{chunk.source_ref}）" if chunk.source_ref else ""
        block = f"### {chunk.title}{cite}\n{chunk.content}".strip()
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


async def retrieve_context(session: AsyncSession, query_text: str) -> str:
    """Convenience: retrieve + format in one call (empty string when nothing)."""
    cfg = get_config_manager().kb
    chunks = await retrieve(session, query_text)
    return format_context(chunks, cfg.KB_MAX_CONTEXT_CHARS)
