"""Reanalysis workflow orchestration.

Keeps the multi-step "re-run AI analysis → update event + duplicates → forward
decision → persist → schedule outbox" workflow out of the API layer, which per
docs/architecture/boundaries.md must not own business workflows, transaction
orchestration, or external delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.webhook_payload import webhook_data_from_mapping
from core.datetime_utils import utcnow
from core.logger import get_logger
from models import WebhookEvent
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.forwarding.outbox import resolve_and_forward, schedule_forward_outbox_many
from services.webhooks.event_context import build_webhook_context
from services.webhooks.forwarding_stage import resolve_forward_decision
from services.webhooks.types import AnalysisResult

logger = get_logger("webhooks.reanalysis_service")


class WebhookEventNotFoundError(LookupError):
    """Raised when the target webhook event does not exist."""


@dataclass(frozen=True, slots=True)
class ReanalysisResult:
    analysis: AnalysisResult
    original_importance: str | None
    new_importance: str | None
    updated_duplicates: int
    should_forward: bool
    outbox_ids: list[int] = field(default_factory=list)

    @property
    def forward_status(self) -> str:
        if self.outbox_ids:
            return "queued"
        return "skipped" if not self.should_forward else "no_target"


async def reanalyze_webhook_event(session: AsyncSession, webhook_id: int) -> ReanalysisResult:
    """Re-run analysis for an event, propagate to duplicates, and forward.

    Owns the transaction commit and the post-commit outbox scheduling so the API
    handler only has to translate the result into a response.
    """
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        raise WebhookEventNotFoundError(str(webhook_id))

    ctx = await build_webhook_context(event)
    res = await analyze_webhook_with_ai(webhook_data_from_mapping(ctx), skip_cache=True)

    old_imp, new_imp = event.importance, res.get("importance")
    event.ai_analysis, event.importance = dict(res), new_imp
    event.processing_status = "completed"

    updated_dups = 0
    if event.is_duplicate is False:
        # Bulk UPDATE instead of loading every duplicate row into the session:
        # an original with many duplicates would otherwise pull them all into
        # memory and emit one UPDATE per row.
        dups_res = await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.duplicate_of == webhook_id)
            .values(ai_analysis=dict(res), importance=new_imp, processing_status="completed", updated_at=utcnow())
        )
        updated_dups = dups_res.rowcount or 0

    fwd_ctx = await build_webhook_context(event)
    decision = await resolve_forward_decision(
        importance=new_imp or "medium",
        is_duplicate=bool(event.is_duplicate),
        noise=None,
        orig=None,
        source=event.source or "unknown",
        parsed_data=cast(dict[str, Any], fwd_ctx.get("parsed_data") or {}),
        session=session,
    )
    outbox_ids: list[int] = []
    if decision.should_forward:
        fwd_result = await resolve_and_forward(
            session=session,
            decision=decision,
            forward_data=fwd_ctx,
            analysis_result=res,
            webhook_id=event.id,
        )
        outbox_ids = list(fwd_result.get("outbox_ids") or [])
    else:
        logger.info("[Reanalysis] 根据规则跳过转发 webhook_id=%s reason=%s", webhook_id, decision.skip_reason)

    source = event.source
    await session.commit()
    await schedule_forward_outbox_many(outbox_ids)
    logger.info(
        "[Reanalysis] 重新分析完成 webhook_id=%s source=%s old_importance=%s new_importance=%s "
        "updated_duplicates=%s outboxes=%s",
        webhook_id,
        source,
        old_imp,
        new_imp,
        updated_dups,
        len(outbox_ids),
    )
    return ReanalysisResult(
        analysis=res,
        original_importance=old_imp,
        new_importance=new_imp,
        updated_duplicates=updated_dups,
        should_forward=decision.should_forward,
        outbox_ids=outbox_ids,
    )
