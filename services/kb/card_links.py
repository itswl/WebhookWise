"""Attach matching KB knowledge to outgoing Feishu alert cards.

The knowledge base previously fed only the AI prompt (RAG). This surfaces it
where the human actually is: the alert card itself gets a small "runbook" block
with the best-matching published entries, so a documented resolution reaches
the on-call reader at notification time — no dashboard visit, no LLM call.

Matching is deliberately cheap and deterministic: token overlap between the
alert's identity/summary and each published doc's title/tags, with a small
bonus when the doc was sedimented from the same source. Failure of any kind
degrades to "no KB block" — this must never delay or break a delivery.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.logger import get_logger
from db.session import session_scope
from models import KBDocument

logger = get_logger("kb.card_links")

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_CANDIDATE_LIMIT = 300
_SNIPPET_CHARS = 160

# Generic tokens that would create fake matches between unrelated docs/alerts.
# CJK entries are BIGRAMS (the tokenizer below emits CJK bigrams — a whole
# Chinese clause is never one token, so content matching works for the
# product's primary language).
_STOP_TOKENS = frozenset(
    {
        "incident",
        "resolution",
        "alert",
        "alarm",
        "webhook",
        "error",
        "high",
        "low",
        "medium",
        "warning",
        "告警",
        "报警",
        "恢复",
        "异常",
        "错误",
        "处理",
        "事件",
        "发生",
        "出现",
    }
)


def _tokens(text: str) -> set[str]:
    """ASCII words plus CJK bigrams, minus generic stopwords.

    CJK has no whitespace word boundaries; bigrams of each han run give
    Chinese titles/summaries real overlap granularity (a single shared bigram
    is common Chinese, hence the stoplist and the match threshold).
    """
    lowered = str(text or "").lower()
    tokens = set(_ASCII_TOKEN_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens - _STOP_TOKENS


def _score(doc_tokens: set[str], doc_source: str, alert_tokens: set[str], source: str) -> float:
    """Content overlap is REQUIRED; same-source is only ever a bonus.

    The source name must not be a token on either side — otherwise every
    sedimented doc trivially matches every alert from its source and the
    freshest two docs get stapled to every card.
    """
    overlap = len(doc_tokens & alert_tokens)
    if overlap == 0:
        return 0.0
    score = float(overlap)
    if doc_source and doc_source == source:
        score += 1.5
    return score


def _snippet(content: str) -> str:
    flat = " ".join(str(content or "").split())
    return flat[:_SNIPPET_CHARS] + ("…" if len(flat) > _SNIPPET_CHARS else "")


async def find_kb_snippets_for_alert(
    *,
    source: str,
    rule_name: str,
    summary: str,
    limit: int = 2,
) -> list[dict[str, str]]:
    """Best-matching published KB entries for an alert, as {title, snippet} dicts."""
    # Deliberately EXCLUDES the source name from the token set (see _score).
    alert_tokens = _tokens(f"{rule_name} {summary}")
    if not alert_tokens:
        return []
    try:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(KBDocument.title, KBDocument.content, KBDocument.tags)
                    .where(KBDocument.status == "published", KBDocument.chunk_index == 0)
                    .order_by(KBDocument.updated_at.desc())
                    .limit(_CANDIDATE_LIMIT)
                )
            ).all()
    except (SQLAlchemyError, OSError, RuntimeError, TypeError, ValueError) as e:
        # OSError included: a refused DB connection can surface unwrapped from
        # the asyncpg transport, and a KB lookup must never fail a delivery.
        logger.warning("[KBCardLinks] Candidate lookup failed (degrading to no KB block): %s", e)
        return []

    scored: list[tuple[float, str, str]] = []
    for title, content, tags in rows:
        tag_map: dict[str, Any] = tags if isinstance(tags, dict) else {}
        # Tag values EXCLUDING "source": the source name must never count as
        # content overlap (it goes through the same-source bonus instead).
        doc_source = str(tag_map.get("source") or "")
        tag_text = " ".join(str(v) for k, v in tag_map.items() if k != "source")
        doc_tokens = (_tokens(title) | _tokens(tag_text)) - _tokens(doc_source)
        score = _score(doc_tokens, doc_source, alert_tokens, source)
        if score >= 2.0:  # at least two meaningful shared tokens (or one + same source)
            scored.append((score, str(title or ""), _snippet(str(content or ""))))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [{"title": title, "snippet": snippet} for _, title, snippet in scored[: max(0, limit)]]
