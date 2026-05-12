"""Forwarding decision and finalization stage for webhook processing."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger
from db.session import session_scope
from models import WebhookEvent
from services.forwarding.outbox import create_forward_outbox_records, schedule_forward_outbox_many
from services.webhooks.command_service import save_webhook_data_in_session
from services.webhooks.decisioning import (
    ForwardingPolicy,
    ForwardRuleSnapshot,
    decide_forwarding,
    normalize_importance,
)
from services.webhooks.policies import forwarding_policy_from_config
from services.webhooks.repository import list_enabled_forward_rules
from services.webhooks.types import (
    AnalysisResolution,
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessContext,
)


async def resolve_forward_decision(
    importance: str,
    is_duplicate: bool,
    beyond_window: bool,
    noise: NoiseReductionContext | None,
    orig: WebhookEvent | None,
    source: str,
    session: AsyncSession | None = None,
    policy: ForwardingPolicy | None = None,
) -> ForwardDecision:
    """Resolve forwarding policy and matching rules for a processed webhook."""
    rules: list[ForwardRuleSnapshot] = []
    try:
        rules = (
            await list_enabled_forward_rules(session=session)
            if session is not None
            else await list_enabled_forward_rules()
        )
    except Exception as e:
        logger.warning("[Forward] 匹配转发规则失败: %s", e)

    decision = decide_forwarding(
        importance=importance,
        is_duplicate=is_duplicate,
        beyond_window=beyond_window,
        noise=noise,
        original_event=orig,
        source=source,
        rules=rules,
        policy=policy or forwarding_policy_from_config(),
    )

    if decision.should_forward:
        logger.debug(
            "[Forward] 决策=转发 is_periodic=%s matched_rules=%d skip_reason=%s",
            decision.is_periodic_reminder,
            len(decision.matched_rules),
            decision.skip_reason,
        )
    else:
        logger.debug("[Forward] 决策=跳过 reason=%s", decision.skip_reason or "no_match")

    return decision


async def execute_forwarding(
    decision: ForwardDecision,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
) -> None:
    """Compatibility wrapper: enqueue forwarding outbox intents."""
    async with session_scope() as session:
        outbox_ids = await create_forward_outbox_records(
            session,
            decision=decision,
            full_data=full_data,
            analysis=analysis,
            webhook_id=webhook_id,
            orig_id=orig_id,
        )
    await schedule_forward_outbox_many(outbox_ids)


async def finalize_analysis_transaction(
    ctx: WebhookProcessContext,
    analysis_res: AnalysisResolution,
    final_analysis: dict[str, Any],
    noise: NoiseReductionContext,
    *,
    forwarding_policy: ForwardingPolicy | None = None,
) -> tuple[Any, ForwardDecision | None, list[int]]:
    """Atomically persist the AI result, final event state and forwarding intents.

    External AI calls intentionally happen before this point. Once a result is
    available, the DB-facing finalization must be all-or-nothing: either the
    webhook record is marked completed with its analysis and all outbox intents
    exist, or none of those writes are committed.
    """
    is_dup_for_save: bool | None = analysis_res.is_duplicate or analysis_res.beyond_window
    original_for_save = analysis_res.original_event
    beyond_for_save = analysis_res.beyond_window
    if original_for_save is None:
        is_dup_for_save = None
        beyond_for_save = False

    async with session_scope() as session:
        save_res = await save_webhook_data_in_session(
            session,
            data=ctx.req_ctx.parsed_data,
            source=ctx.req_ctx.source,
            raw_payload=ctx.req_ctx.payload,
            headers=ctx.req_ctx.headers,
            client_ip=ctx.req_ctx.client_ip,
            ai_analysis=final_analysis,
            alert_hash=ctx.alert_hash,
            is_duplicate=is_dup_for_save,
            original_event=original_for_save,
            beyond_window=beyond_for_save,
            reanalyzed=analysis_res.reanalyzed,
            event_id=ctx.event_id,
        )

        fwd_dec = await resolve_forward_decision(
            normalize_importance(final_analysis.get("importance", "")),
            save_res.is_duplicate and not save_res.beyond_window,
            save_res.beyond_window,
            noise,
            analysis_res.original_event,
            ctx.req_ctx.source,
            session=session,
            policy=forwarding_policy,
        )
        outbox_ids = await create_forward_outbox_records(
            session,
            decision=fwd_dec,
            full_data=ctx.req_ctx.webhook_full_data,
            analysis=final_analysis,
            webhook_id=save_res.webhook_id,
            orig_id=save_res.original_id,
        )
        return save_res, fwd_dec, outbox_ids
