"""Silence debt: surface silences that hide a still-firing chronic problem.

A permanent (no-expiry) silence over a source that keeps firing is technical
debt, not a fix — the underlying alert is still triggering; it is just being
swallowed. This analytic turns the silence set into an accountability view:
how much each active silence has suppressed over a trailing window, its daily
rate, and a "chronic" flag for the no-expiry silences carrying real volume.

It reuses the windowed ``get_silence_suppression_counts`` aggregate (one
index-backed GROUP BY) rather than scanning events, so it stays cheap enough to
back both a dashboard panel and the periodic report.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.silences.store import list_silences
from services.webhooks.decision_trace_queries import get_silence_suppression_counts

# A no-expiry silence that has suppressed at least this many alerts over the
# window is "chronic": it is masking a source that never stopped firing, which
# is exactly the case the noise-center's tune_threshold recommendation exists
# for. Tuned to flag the real offenders, not a briefly-busy legitimate mute.
_CHRONIC_MIN_SUPPRESSED = 500

# Same assumption the noise center uses to translate avoided notifications into
# a human-time figure, kept consistent so the two surfaces agree.
_MINUTES_PER_AVOIDED_NOTIFICATION = 3


def _silence_label(silence: Any) -> str:
    # The operator's own comment is the most recognizable identifier (e.g.
    # "perm: GPU comfyui-model02-n01"); fall back to the match criteria, then id.
    if silence.comment:
        return str(silence.comment)
    parts = [p for p in (silence.match_source, silence.match_payload) if p]
    return " / ".join(parts) or f"silence #{silence.id}"


async def get_silence_debt(session: AsyncSession, *, window_days: int = 30) -> dict[str, Any]:
    """Rank active silences by suppression volume over the trailing window.

    Returns per-silence rows (newest-suppression-heavy first) plus rollups: how
    many silences are chronic, total alerts suppressed, and the estimated
    operator time the mutes saved. ``chronic`` is the actionable signal — a
    no-expiry silence still swallowing volume that should become an upstream fix.
    """
    window_days = max(1, int(window_days))
    window = timedelta(days=window_days)

    silences = await list_silences(session, active_only=True)
    counts = await get_silence_suppression_counts(session, silence_ids=[int(s.id) for s in silences], window=window)

    items: list[dict[str, Any]] = []
    total_suppressed = 0
    chronic_count = 0
    for silence in silences:
        stats = counts.get(int(silence.id), {})
        suppressed = int(stats.get("count", 0) or 0)
        total_suppressed += suppressed
        no_expiry = silence.expires_at is None
        chronic = no_expiry and suppressed >= _CHRONIC_MIN_SUPPRESSED
        if chronic:
            chronic_count += 1
        items.append(
            {
                "silence_id": int(silence.id),
                "label": _silence_label(silence),
                "comment": silence.comment or "",
                "match_source": silence.match_source or "",
                "match_payload": silence.match_payload or "",
                "no_expiry": no_expiry,
                "suppressed": suppressed,
                "daily_rate": round(suppressed / window_days, 1),
                "last_suppressed_at": stats.get("last_suppressed_at"),
                "chronic": chronic,
            }
        )

    items.sort(key=lambda item: item["suppressed"], reverse=True)
    return {
        "window_days": window_days,
        "active_silences": len(silences),
        "chronic_count": chronic_count,
        "total_suppressed": total_suppressed,
        "estimated_minutes_saved": total_suppressed * _MINUTES_PER_AVOIDED_NOTIFICATION,
        "silences": items,
    }


def summarize_silence_debt(debt: dict[str, Any]) -> str | None:
    """One-line digest for the periodic report, or None when there is no debt.

    Names the single worst chronic silence so the report points at an owner /
    action rather than just a number.
    """
    chronic = [item for item in debt.get("silences", []) if item.get("chronic")]
    if not chronic:
        return None
    worst = chronic[0]
    extra = f" (+{len(chronic) - 1} more)" if len(chronic) > 1 else ""
    return (
        f"Chronic silences: {len(chronic)} no-expiry mute(s) hid "
        f"{sum(item['suppressed'] for item in chronic)} alerts over {debt['window_days']}d — "
        f"top: {worst['label']} ({worst['suppressed']}, ~{worst['daily_rate']}/day){extra}. "
        "A still-firing source is being swallowed; fix upstream or set an expiry."
    )


__all__ = ["get_silence_debt", "summarize_silence_debt"]
