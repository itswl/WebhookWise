"""Forwarding decision and finalization stage for webhook processing."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger
from db.session import session_scope
from models import WebhookEvent
from services.forwarding.forward import forward_to_openclaw, forward_to_remote
from services.forwarding.policies import ForwardOutboxPolicy
from services.webhooks.command_service import save_webhook_data_in_session
from services.webhooks.decisioning import (
    ForwardingPolicy,
    ForwardRuleSnapshot,
    decide_forwarding,
    normalize_importance,
)
from services.webhooks.deduplication import remember_duplicate_source
from services.webhooks.policies import forwarding_policy_from_config
from services.webhooks.repository import create_openclaw_analysis, list_enabled_forward_rules, mark_last_notified
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
    """Compatibility wrapper: directly dispatch forwarding in the worker."""
    await dispatch_forwarding_decision(
        decision,
        full_data=full_data,
        analysis=analysis,
        webhook_id=webhook_id,
        orig_id=orig_id,
    )


def _target_rules(decision: ForwardDecision) -> list[dict[str, Any]]:
    if decision.matched_rules:
        return [dict(rule) for rule in decision.matched_rules]
    return [ForwardOutboxPolicy.from_config().default_rule()]


def _is_forward_success(result: dict[str, Any]) -> bool:
    return result.get("status") == "success" or bool(result.get("_pending"))


async def _dispatch_one_target(
    rule: dict[str, Any],
    *,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    is_periodic_reminder: bool,
) -> dict[str, Any] | None:
    target_type = str(rule.get("target_type", "webhook") or "webhook")
    target_url = str(rule.get("target_url", "") or "")
    if target_type == "openclaw":
        result = await forward_to_openclaw(full_data, analysis)
        if result.get("_pending"):
            await create_openclaw_analysis(
                webhook_id,
                run_id=str(result.get("_openclaw_run_id", "")),
                session_key=str(result.get("_openclaw_session_key", "")),
            )
    else:
        if not target_url:
            logger.warning("[Forward] 规则 '%s' target_url 为空，跳过直接转发", rule.get("name", rule.get("id")))
            return None
        result = await forward_to_remote(
            webhook_data=full_data,
            analysis_result=analysis,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
        )

    if not _is_forward_success(result):
        raise RuntimeError(f"forward status={result.get('status')}: {result.get('message', '')}")
    return result


async def dispatch_forwarding_decision(
    decision: ForwardDecision | None,
    *,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
) -> list[dict[str, Any]]:
    """Execute forwarding directly in the webhook worker.

    The DB no longer acts as a forwarding queue. TaskIQ/Redis owns retry of the
    worker message; PostgreSQL only records the final event and notification
    timestamp.
    """
    if not decision or not decision.should_forward:
        return []

    results: list[dict[str, Any]] = []
    for rule in _target_rules(decision):
        result = await _dispatch_one_target(
            rule,
            full_data=full_data,
            analysis=analysis,
            webhook_id=webhook_id,
            is_periodic_reminder=decision.is_periodic_reminder,
        )
        if result is not None:
            results.append(result)
    if results:
        await mark_last_notified(orig_id or webhook_id)
    return results


async def finalize_analysis_transaction(
    ctx: WebhookProcessContext,
    analysis_res: AnalysisResolution,
    final_analysis: dict[str, Any],
    noise: NoiseReductionContext,
    *,
    forwarding_policy: ForwardingPolicy | None = None,
) -> tuple[Any, ForwardDecision | None]:
    """Persist the AI result and final event state.

    PostgreSQL is no longer used as a forwarding queue here. External forwarding
    happens after this transaction in the worker, letting TaskIQ/Redis own retry
    semantics and keeping the DB write path short.
    """
    is_dup_for_save: bool | None = analysis_res.is_duplicate or analysis_res.beyond_window
    original_for_save = analysis_res.original_event
    original_id_for_save = analysis_res.original_event_id or (original_for_save.id if original_for_save else None)
    beyond_for_save = analysis_res.beyond_window
    skip_duplicate_lookup = bool(analysis_res.is_reused and original_for_save is None and original_id_for_save)
    if original_for_save is None and original_id_for_save is None:
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
            original_event_id=original_id_for_save,
            beyond_window=beyond_for_save,
            reanalyzed=analysis_res.reanalyzed,
            event_id=ctx.event_id,
            skip_duplicate_lookup=skip_duplicate_lookup,
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
    await remember_duplicate_source(
        ctx.alert_hash,
        original_event_id=save_res.original_id or save_res.webhook_id,
        analysis=final_analysis,
    )
    return save_res, fwd_dec
