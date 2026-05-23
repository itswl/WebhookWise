"""Forwarding decision and finalization stage for webhook processing."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.observability.events import add_span_event, emit_event
from core.observability.tracing import otel_span
from core.sensitive_data import redact_headers
from db.session import session_scope
from models import WebhookEvent
from services.dedup import DedupResult
from services.forwarding.outbox import resolve_and_forward
from services.webhooks.command_service import SaveWebhookInput, SaveWebhookResult, save_webhook_data_in_session
from services.webhooks.decisioning import (
    ForwardDecision,
    ForwardingPolicy,
    ForwardRuleSnapshot,
    decide_forwarding,
    forwarding_policy_from_config,
    normalize_importance,
)
from services.webhooks.repository import list_enabled_forward_rules
from services.webhooks.types import (
    AnalysisResult,
    NoiseReductionContext,
    WebhookProcessContext,
)

logger = get_logger("webhooks.forwarding_stage")


@dataclass(frozen=True, slots=True)
class FinalizeAnalysisResult:
    save_result: SaveWebhookResult
    forward_decision: ForwardDecision | None
    outbox_ids: list[int]


async def resolve_forward_decision(
    importance: str,
    is_duplicate: bool,
    noise: NoiseReductionContext | None,
    orig: WebhookEvent | None,
    source: str,
    parsed_data: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
    policy: ForwardingPolicy | None = None,
    event_type: str = "webhook_forward",
) -> ForwardDecision:
    """Resolve forwarding policy and matching rules for a processed webhook."""
    rules: list[ForwardRuleSnapshot] = []
    try:
        rules = await list_enabled_forward_rules(session=session)
    except Exception as e:
        logger.warning("[Forward] 匹配转发规则失败: %s", e)

    decision = decide_forwarding(
        event_type=event_type,
        importance=importance,
        is_duplicate=is_duplicate,
        noise=noise,
        original_event=orig,
        source=source,
        rules=rules,
        policy=policy or forwarding_policy_from_config(),
        parsed_data=parsed_data,
    )

    if decision.should_forward:
        logger.info(
            "[Forward] 决策=转发 source=%s importance=%s duplicate=%s is_periodic=%s matched_rules=%d",
            source,
            importance,
            is_duplicate,
            decision.is_periodic_reminder,
            len(decision.matched_rules),
        )
    else:
        logger.info(
            "[Forward] 决策=跳过 source=%s importance=%s duplicate=%s reason=%s",
            source,
            importance,
            is_duplicate,
            decision.skip_reason or "no_match",
        )

    return decision


async def finalize_analysis_transaction(
    ctx: WebhookProcessContext,
    analysis_res: DedupResult,
    final_analysis: AnalysisResult,
    noise: NoiseReductionContext,
    *,
    forwarding_policy: ForwardingPolicy | None = None,
) -> FinalizeAnalysisResult:
    """Persist the AI result and final event state.

    Forwarding intents are persisted in the same transaction as the processed
    webhook state. The network side effect happens later in an outbox worker.
    """
    # resolve_dedup 已经是权威结果（Redis + DB fallback），直接信任
    is_dup_for_save = analysis_res.is_duplicate
    original_id_for_save = analysis_res.original_event_id
    skip_duplicate_lookup = True

    outbox_ids: list[int] = []
    persist_attrs = {
        "event_id": ctx.event_id or 0,
        "source": ctx.req_ctx.source,
        "alert_hash": ctx.alert_hash[:12],
        "pipeline.step": "persist",
    }
    with otel_span("webhook.persist", persist_attrs):
        async with session_scope() as session:
            save_res = await save_webhook_data_in_session(
                session,
                input=SaveWebhookInput(
                    data=ctx.req_ctx.parsed_data,
                    source=ctx.req_ctx.source,
                    raw_payload=ctx.req_ctx.payload,
                    headers=ctx.req_ctx.headers,
                    client_ip=ctx.req_ctx.client_ip,
                    request_id=ctx.request_id,
                    ai_analysis=final_analysis,
                    alert_hash=ctx.alert_hash,
                    dedup_key=ctx.dedup_key,
                    is_duplicate=is_dup_for_save,
                    original_event_id=original_id_for_save,
                    skip_duplicate_lookup=skip_duplicate_lookup,
                ),
            )

            decision_original = None
            if save_res.original_id is not None:
                decision_original = await session.get(WebhookEvent, save_res.original_id)
            fwd_dec = await resolve_forward_decision(
                normalize_importance(final_analysis.get("importance", "")),
                save_res.is_duplicate,
                noise,
                decision_original,
                ctx.req_ctx.source,
                parsed_data=ctx.req_ctx.parsed_data,
                session=session,
                policy=forwarding_policy,
            )

            if fwd_dec.should_forward:
                forward_data = dict(ctx.req_ctx.webhook_full_data)
                if isinstance(forward_data.get("headers"), dict):
                    forward_data["headers"] = redact_headers(forward_data["headers"])
                first_target_type = fwd_dec.matched_rules[0].target_type if fwd_dec.matched_rules else "default"

                with otel_span(
                    "webhook.persist.outbox",
                    {
                        **persist_attrs,
                        "forward.target_count": len(fwd_dec.matched_rules) or 1,
                        "forward.target_type": first_target_type,
                    },
                ):
                    fwd_result = await resolve_and_forward(
                        session=session,
                        decision=fwd_dec,
                        forward_data=forward_data,
                        analysis_result=final_analysis,
                        webhook_id=save_res.webhook_id,
                        orig_id=save_res.original_id,
                    )
                    outbox_ids = list(fwd_result.get("outbox_ids") or [])
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

    return FinalizeAnalysisResult(save_res, fwd_dec, outbox_ids)
