"""Forwarding decision and finalization stage for webhook processing."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger
from core.observability.events import add_span_event, emit_event
from core.observability.tracing import span as otel_span
from core.sensitive_data import redact_headers
from db.session import session_scope
from models import WebhookEvent
from services.forwarding.outbox import create_forward_outbox_records, schedule_forward_outbox_many
from services.webhooks.command_service import SaveWebhookResult, save_webhook_data_in_session
from services.webhooks.decisioning import (
    ForwardingPolicy,
    ForwardRuleSnapshot,
    decide_forwarding,
    normalize_importance,
)
from services.webhooks.deduplication import remember_duplicate_source
from services.webhooks.policies import forwarding_policy_from_config
from services.webhooks.repository import list_enabled_forward_rules
from services.webhooks.types import (
    AnalysisResolution,
    ForwardDecision,
    NoiseReductionContext,
    WebhookProcessContext,
)


@dataclass(frozen=True, slots=True)
class FinalizeAnalysisResult:
    save_result: SaveWebhookResult
    forward_decision: ForwardDecision | None
    outbox_ids: list[int]


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
        logger.info(
            "[Forward] 决策=转发 source=%s importance=%s duplicate=%s beyond_window=%s is_periodic=%s matched_rules=%d",
            source,
            importance,
            is_duplicate,
            beyond_window,
            decision.is_periodic_reminder,
            len(decision.matched_rules),
        )
    else:
        logger.info(
            "[Forward] 决策=跳过 source=%s importance=%s duplicate=%s beyond_window=%s reason=%s",
            source,
            importance,
            is_duplicate,
            beyond_window,
            decision.skip_reason or "no_match",
        )

    return decision


async def finalize_analysis_transaction(
    ctx: WebhookProcessContext,
    analysis_res: AnalysisResolution,
    final_analysis: dict[str, Any],
    noise: NoiseReductionContext,
    *,
    forwarding_policy: ForwardingPolicy | None = None,
) -> FinalizeAnalysisResult:
    """Persist the AI result and final event state.

    Forwarding intents are persisted in the same transaction as the processed
    webhook state. The network side effect happens later in an outbox worker.
    """
    is_dup_for_save: bool | None = analysis_res.is_duplicate or analysis_res.beyond_window
    original_for_save = analysis_res.original_event
    original_id_for_save = analysis_res.original_event_id or (original_for_save.id if original_for_save else None)
    beyond_for_save = analysis_res.beyond_window
    skip_duplicate_lookup = bool(analysis_res.is_reused and original_for_save is None and original_id_for_save)
    if original_for_save is None and original_id_for_save is None:
        is_dup_for_save = None
        beyond_for_save = False

    outbox_ids: list[int] = []
    with otel_span(
        "webhook.persist",
        {
            "event_id": ctx.event_id or 0,
            "source": ctx.req_ctx.source,
            "alert_hash": ctx.alert_hash[:12],
            "pipeline.step": "persist",
        },
    ):
        async with session_scope() as session:
            with otel_span(
                "webhook.persist.save",
                {
                    "event_id": ctx.event_id or 0,
                    "source": ctx.req_ctx.source,
                    "alert_hash": ctx.alert_hash[:12],
                    "pipeline.step": "persist",
                },
            ):
                save_res = await save_webhook_data_in_session(
                    session,
                    data=ctx.req_ctx.parsed_data,
                    source=ctx.req_ctx.source,
                    raw_payload=ctx.req_ctx.payload,
                    headers=ctx.req_ctx.headers,
                    client_ip=ctx.req_ctx.client_ip,
                    request_id=ctx.request_id,
                    ai_analysis=final_analysis,
                    alert_hash=ctx.alert_hash,
                    is_duplicate=is_dup_for_save,
                    original_event=original_for_save,
                    original_event_id=original_id_for_save,
                    beyond_window=beyond_for_save,
                    reanalyzed=analysis_res.reanalyzed,
                    skip_duplicate_lookup=skip_duplicate_lookup,
                )

            with otel_span(
                "webhook.persist.forward_decision",
                {
                    "event_id": save_res.webhook_id,
                    "source": ctx.req_ctx.source,
                    "pipeline.step": "persist",
                    "webhook.importance": normalize_importance(final_analysis.get("importance", "")),
                    "webhook.suppressed": save_res.is_duplicate and not save_res.beyond_window,
                },
            ):
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
            if fwd_dec.should_forward:
                forward_data = dict(ctx.req_ctx.webhook_full_data)
                if isinstance(forward_data.get("headers"), dict):
                    forward_data["headers"] = redact_headers(forward_data["headers"])
                with otel_span(
                    "webhook.persist.outbox",
                    {
                        "event_id": save_res.webhook_id,
                        "source": ctx.req_ctx.source,
                        "pipeline.step": "persist",
                        "forward.target_count": len(fwd_dec.matched_rules) or 1,
                        "forward.target_type": (
                            str((fwd_dec.matched_rules[0] if fwd_dec.matched_rules else {}).get("target_type") or "")
                            or "default"
                        ),
                    },
                ):
                    outbox_ids = await create_forward_outbox_records(
                        session,
                        decision=fwd_dec,
                        full_data=forward_data,
                        analysis=final_analysis,
                        webhook_id=save_res.webhook_id,
                        orig_id=save_res.original_id,
                    )
                emit_event(
                    "forward.outbox.queued",
                    {
                        "event_id": save_res.webhook_id,
                        "source": ctx.req_ctx.source,
                        "alert_hash": ctx.alert_hash[:12],
                        "forward.target_count": len(outbox_ids),
                        "forward.periodic_reminder": fwd_dec.is_periodic_reminder,
                    },
                )
            else:
                add_span_event(
                    "forward.decision.skipped",
                    {
                        "event_id": save_res.webhook_id,
                        "source": ctx.req_ctx.source,
                        "alert_hash": ctx.alert_hash[:12],
                        "forward.skip_reason": fwd_dec.skip_reason or "unknown",
                    },
                )
    await remember_duplicate_source(
        ctx.alert_hash,
        original_event_id=save_res.original_id or save_res.webhook_id,
        analysis=final_analysis,
    )
    await schedule_forward_outbox_many(outbox_ids)
    return FinalizeAnalysisResult(save_res, fwd_dec, outbox_ids)
