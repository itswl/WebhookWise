"""Embedding generation for the knowledge base.

Two backends behind one ``embed_texts`` interface:
- Real: an OpenAI-compatible ``/embeddings`` endpoint (KB_EMBEDDING_API_URL/KEY/
  MODEL). The main OPENAI_API_URL is intentionally NOT reused — it is usually
  OpenRouter, which has no embeddings route.
- Placeholder: a deterministic, hash-derived unit vector used when no endpoint
  is configured, so the whole ingest→retrieve→inject pipeline runs and tests
  end to end without an external service. Placeholder vectors only match
  themselves (no semantics), so retrieval quality is low until a real endpoint
  is set — callers surface this via the ``placeholder`` model marker.
"""

from __future__ import annotations

import hashlib
import math

from core.app_context import get_config_manager
from core.logger import get_logger

logger = get_logger("kb.embedding")

PLACEHOLDER_MODEL = "placeholder"


def _placeholder_vector(text: str, dim: int) -> list[float]:
    """Deterministic unit vector from text bytes — stable across runs/processes.

    Hashes (index, text) blocks to fill ``dim`` floats, centers to [-0.5, 0.5),
    then L2-normalizes. Identical text → identical vector (so a query matches an
    identically-worded chunk), but it carries no semantic meaning.
    """
    raw = bytearray()
    counter = 0
    while len(raw) < dim * 2:
        block = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        raw.extend(block)
        counter += 1
    vec = [((raw[i] << 8 | raw[i + 1]) / 65535.0) - 0.5 for i in range(0, dim * 2, 2)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def is_placeholder_active() -> bool:
    """True when no real embedding endpoint is configured."""
    return not bool(get_config_manager().kb.KB_EMBEDDING_API_URL.strip())


def active_embedding_model() -> str:
    cfg = get_config_manager().kb
    return PLACEHOLDER_MODEL if is_placeholder_active() else cfg.KB_EMBEDDING_MODEL


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed a batch of texts. Returns (vectors, model_name).

    Falls back to the placeholder embedding if no endpoint is configured or the
    real call fails — embedding must never hard-fail KB ingest or an alert
    analysis; degraded retrieval is acceptable, a crash is not.
    """
    cfg = get_config_manager().kb
    if not texts:
        return [], active_embedding_model()

    if is_placeholder_active():
        return [_placeholder_vector(t, cfg.KB_VECTOR_DIM) for t in texts], PLACEHOLDER_MODEL

    try:
        return await _embed_via_endpoint(texts), cfg.KB_EMBEDDING_MODEL
    except Exception as exc:  # noqa: BLE001 - embedding is best-effort, never a gate
        logger.warning("[KB] Embedding endpoint failed, falling back to placeholder: %s", exc)
        return [_placeholder_vector(t, cfg.KB_VECTOR_DIM) for t in texts], PLACEHOLDER_MODEL


async def _embed_via_endpoint(texts: list[str]) -> list[list[float]]:
    from openai import AsyncOpenAI

    from core.http_client import get_http_client

    cfg = get_config_manager().kb
    client = AsyncOpenAI(
        api_key=cfg.KB_EMBEDDING_API_KEY or "placeholder-key",
        base_url=cfg.KB_EMBEDDING_API_URL,
        http_client=get_http_client(),
        timeout=cfg.KB_EMBEDDING_TIMEOUT_SECONDS,
    )
    resp = await client.embeddings.create(model=cfg.KB_EMBEDDING_MODEL, input=texts)
    # Preserve input order (OpenAI returns items with an `index`).
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [list(item.embedding) for item in ordered]
