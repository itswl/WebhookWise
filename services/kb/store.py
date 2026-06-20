"""Knowledge-base ingestion: chunk a document, embed, upsert idempotently."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.logger import get_logger
from models import KBDocument
from services.kb.embedding import embed_texts

logger = get_logger("kb.store")


@dataclass(frozen=True, slots=True)
class IngestResult:
    title: str
    chunks: int
    embedding_model: str


def _content_hash(title: str, chunk_index: int, content: str) -> str:
    return hashlib.sha256(f"{title}\x00{chunk_index}\x00{content}".encode()).hexdigest()


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of <= max_chars, preferring paragraph boundaries.

    Splits on blank lines first; a paragraph longer than max_chars is hard-split.
    Keeps chunks reasonably semantic so each carries a coherent fact.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(para[i : i + max_chars] for i in range(0, len(para), max_chars))
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > max_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks or ([text.strip()] if text.strip() else [])


async def ingest_document(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    source_ref: str | None = None,
    tags: dict[str, Any] | None = None,
) -> IngestResult:
    """Chunk → embed → upsert one document. Idempotent by content_hash.

    Re-ingesting an identical chunk updates it in place (ON CONFLICT) rather than
    duplicating. Chunks of this title that no longer exist (content changed and
    shrank) are pruned so the KB reflects the latest version.
    """
    cfg = get_config_manager().kb
    chunks = chunk_text(content, cfg.KB_CHUNK_MAX_CHARS)
    if not chunks:
        return IngestResult(title=title, chunks=0, embedding_model="")

    vectors, model = await embed_texts(chunks)
    now_hashes: list[str] = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
        chash = _content_hash(title, idx, chunk)
        now_hashes.append(chash)
        stmt = pg_insert(KBDocument).values(
            title=title,
            source_ref=source_ref,
            chunk_index=idx,
            content=chunk,
            content_hash=chash,
            embedding=vector,
            embedding_model=model,
            tags=tags,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["content_hash"],
            set_={
                "title": title,
                "source_ref": source_ref,
                "chunk_index": idx,
                "embedding": vector,
                "embedding_model": model,
                "tags": tags,
            },
        )
        await session.execute(stmt)

    # Prune stale chunks of this title (e.g. the doc got shorter on re-ingest).
    await session.execute(
        delete(KBDocument).where(KBDocument.title == title, KBDocument.content_hash.notin_(now_hashes))
    )
    logger.info("[KB] Ingested document title=%s chunks=%d model=%s", title, len(chunks), model)
    return IngestResult(title=title, chunks=len(chunks), embedding_model=model)


async def count_documents(session: AsyncSession) -> int:
    from sqlalchemy import func

    return int((await session.execute(select(func.count(KBDocument.id)))).scalar() or 0)
