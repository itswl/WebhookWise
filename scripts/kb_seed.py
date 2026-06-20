"""Seed the knowledge base from markdown files in knowledge_base/seed/.

Each file may carry a YAML frontmatter block (--- ... ---) with `title`,
`source_ref`, and arbitrary tag keys (service/project/owner/...); the body after
the frontmatter is the document content. Run inside the app environment:

    python -m scripts.kb_seed                     # ingest all seed/*.md
    python -m scripts.kb_seed path/to/doc.md ...  # ingest specific files

Idempotent: re-running upserts by content hash. Uses the configured embedding
backend (placeholder when no KB_EMBEDDING_* endpoint is set).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

SEED_DIR = Path(__file__).resolve().parent.parent / "knowledge_base" / "seed"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), body


def _to_doc(path: Path) -> dict[str, Any]:
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    title = str(meta.get("title") or path.stem)
    source_ref = meta.get("source_ref")
    # Remaining frontmatter keys become string facet tags; a list value (e.g.
    # `tags: [a, b]`) is joined into a comma string rather than stringified as a
    # Python list repr.
    tags: dict[str, str] = {}
    for key, value in meta.items():
        if key in ("title", "source_ref") or value is None:
            continue
        tags[key] = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return {"title": title, "content": body, "source_ref": source_ref, "tags": tags or None}


async def _run(paths: list[Path]) -> None:
    from core.app_context import get_default_app_context, init_default_app_context, set_default_app_context
    from db.session import session_scope
    from services.kb.store import ingest_document

    # Bootstrap the default AppContext (DB engine/session factory) when run as a
    # standalone CLI; inside the app/worker process it is already initialized.
    if get_default_app_context() is None:
        set_default_app_context(init_default_app_context())

    total_chunks = 0
    for path in paths:
        doc = _to_doc(path)
        async with session_scope() as session:
            result = await ingest_document(
                session,
                title=doc["title"],
                content=doc["content"],
                source_ref=doc["source_ref"],
                tags=doc["tags"],
            )
        total_chunks += result.chunks
        print(f"  ingested: {result.title} -> {result.chunks} chunks (model={result.embedding_model})")
    print(f"Done. {len(paths)} document(s), {total_chunks} chunk(s).")


def main() -> None:
    args = sys.argv[1:]
    paths = [Path(a) for a in args] if args else sorted(SEED_DIR.glob("*.md"))
    if not paths:
        print(f"No seed documents found in {SEED_DIR}")
        return
    print(f"Seeding {len(paths)} knowledge-base document(s)...")
    asyncio.run(_run(paths))


if __name__ == "__main__":
    main()
