"""Forwarding decision and finalization stage for webhook processing."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger, mask_url
from core.otel import span as otel_span
from core.sensitive_data import redact_headers
from db.session import session_scope
from models import WebhookEvent
from services.forwarding.forward import forward_to_openclaw as default_forward_to_openclaw
from services.forwarding.forward import forward_to_remote as default_forward_to_remote
from services.forwarding.outbox import create_forward_outbox_records, schedule_forward_outbox_many
from services.forwarding.policies import ForwardOutboxPolicy
from services.webhooks.command_service import SaveWebhookResult, save_webhook_data_in_session
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


class ForwardingClient(Protocol):
    async def forward_to_remote(
        self,
        *,
        webhook_data: dict[str, Any],
        analysis_result: dict[str, Any],
        target_url: str,
        is_periodic_reminder: bool,
    ) -> dict[str, Any]: ...

    async def forward_to_openclaw(
        self,
        *,
        webhook_data: dict[str, Any],
        analysis_result: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DefaultForwardingClient:
    async def forward_to_remote(
        self,
        *,
        webhook_data: dict[str, Any],
        analysis_result: dict[str, Any],
        target_url: str,
        is_periodic_reminder: bool,
    ) -> dict[str, Any]:
        return await default_forward_to_remote(
            webhook_data=webhook_data,
            analysis_result=analysis_result,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
        )

    async def forward_to_openclaw(
        self,
        *,
        webhook_data: dict[str, Any],
        analysis_result: dict[str, Any],
    ) -> dict[str, Any]:
        return await default_forward_to_openclaw(webhook_data, analysis_result)


@dataclass(frozen=True, slots=True)
class FinalizeAnalysisResult:
    save_result: SaveWebhookResult
    forward_decision: ForwardDecision | None
    outbox_ids: list[int]

    def __iter__(self) -> Iterator[Any]:
        # Backward-compatible two-value unpacking for existing tests/callers.
        yield self.save_result
        yield self.forward_decision


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


async def execute_forwarding(
    decision: ForwardDecision,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
    forwarding_client: ForwardingClient | None = None,
) -> None:
    """Compatibility wrapper: directly dispatch forwarding in the worker."""
    await dispatch_forwarding_decision(
        decision,
        full_data=full_data,
        analysis=analysis,
        webhook_id=webhook_id,
        orig_id=orig_id,
        forwarding_client=forwarding_client,
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
    forwarding_client: ForwardingClient,
) -> dict[str, Any] | None:
    target_type = str(rule.get("target_type", "webhook") or "webhook")
    target_url = str(rule.get("target_url", "") or "")
    rule_label = rule.get("name") or rule.get("id") or "default"
    logger.info(
        "[Forward] 开始执行目标 event_id=%s rule=%s target_type=%s target=%s periodic=%s",
        webhook_id,
        rule_label,
        target_type,
        mask_url(target_url) if target_url else "",
        is_periodic_reminder,
    )
    if target_type == "openclaw":
        result = await forwarding_client.forward_to_openclaw(webhook_data=full_data, analysis_result=analysis)
        if result.get("_pending"):
            analysis_id = await create_openclaw_analysis(
                webhook_id,
                run_id=str(result.get("_openclaw_run_id", "")),
                session_key=str(result.get("_openclaw_session_key", "")),
            )
            logger.info("[Forward] OpenClaw 分析记录已创建 event_id=%s analysis_id=%s", webhook_id, analysis_id)
    else:
        if not target_url:
            logger.warning("[Forward] 规则 '%s' target_url 为空，跳过直接转发", rule.get("name", rule.get("id")))
            return None
        result = await forwarding_client.forward_to_remote(
            webhook_data=full_data,
            analysis_result=analysis,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
        )

    if not _is_forward_success(result):
        raise RuntimeError(f"forward status={result.get('status')}: {result.get('message', '')}")
    logger.info(
        "[Forward] 目标执行完成 event_id=%s rule=%s target_type=%s status=%s",
        webhook_id,
        rule_label,
        target_type,
        result.get("status") or ("pending" if result.get("_pending") else "unknown"),
    )
    return result


async def dispatch_forwarding_decision(
    decision: ForwardDecision | None,
    *,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
    forwarding_client: ForwardingClient | None = None,
) -> list[dict[str, Any]]:
    """Legacy compatibility wrapper for tests/manual callers that need direct forwarding."""
    if not decision or not decision.should_forward:
        logger.info(
            "[Forward] 无需转发 event_id=%s reason=%s", webhook_id, getattr(decision, "skip_reason", "no_decision")
        )
        return []

    with otel_span("forward.dispatch", {"event_id": webhook_id, "forward.status": "started"}):
        client = forwarding_client or DefaultForwardingClient()
        results: list[dict[str, Any]] = []
        for rule in _target_rules(decision):
            result = await _dispatch_one_target(
                rule,
                full_data=full_data,
                analysis=analysis,
                webhook_id=webhook_id,
                is_periodic_reminder=decision.is_periodic_reminder,
                forwarding_client=client,
            )
            if result is not None:
                results.append(result)
    if results:
        await mark_last_notified(orig_id or webhook_id)
        logger.info(
            "[Forward] 转发批次完成 event_id=%s notified_event_id=%s target_count=%d",
            webhook_id,
            orig_id or webhook_id,
            len(results),
        )
    return results


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
        {"event_id": ctx.event_id or 0, "source": ctx.req_ctx.source, "alert_hash": ctx.alert_hash[:12]},
    ):
        async with session_scope() as session:
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
            if fwd_dec.should_forward:
                forward_data = dict(ctx.req_ctx.webhook_full_data)
                if isinstance(forward_data.get("headers"), dict):
                    forward_data["headers"] = redact_headers(forward_data["headers"])
                outbox_ids = await create_forward_outbox_records(
                    session,
                    decision=fwd_dec,
                    full_data=forward_data,
                    analysis=final_analysis,
                    webhook_id=save_res.webhook_id,
                    orig_id=save_res.original_id,
                )
    await remember_duplicate_source(
        ctx.alert_hash,
        original_event_id=save_res.original_id or save_res.webhook_id,
        analysis=final_analysis,
    )
    await schedule_forward_outbox_many(outbox_ids)
    return FinalizeAnalysisResult(save_res, fwd_dec, outbox_ids)
