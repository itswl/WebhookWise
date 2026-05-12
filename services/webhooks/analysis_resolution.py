"""AI analysis resolution stage for webhook processing."""

from datetime import datetime
from typing import Any

from core.logger import logger
from db.session import session_scope
from services.analysis.ai_analyzer import analyze_webhook_with_ai, log_ai_usage
from services.webhooks.policies import AnalysisResolutionPolicy
from services.webhooks.repository import check_duplicate_event
from services.webhooks.types import AnalysisResolution


async def resolve_analysis(
    alert_hash: str,
    full_data: dict[str, Any],
    *,
    policy: AnalysisResolutionPolicy | None = None,
    http_client: Any | None = None,
) -> AnalysisResolution:
    policy = policy or AnalysisResolutionPolicy.from_config()
    async with session_scope() as session:
        check = await check_duplicate_event(
            alert_hash, session=session, time_window_hours=policy.duplicate_window_hours
        )
    orig, last_beyond = check.original_event, check.last_beyond_window_event

    if check.beyond_window and orig:
        if (
            last_beyond
            and last_beyond.created_at
            and (datetime.now() - last_beyond.created_at).total_seconds() < policy.recent_beyond_window_reuse_seconds
            and not (last_beyond.ai_analysis or {}).get("_degraded")
        ):
            logger.debug(
                "[Pipeline] 窗口外复用最近 beyond_window 事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12]
            )
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution(
                {**(last_beyond.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True
            )
        if not policy.reanalyze_after_time_window and not (orig.ai_analysis or {}).get("_degraded"):
            logger.debug("[Pipeline] 窗口外复用原始事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**(orig.ai_analysis or {}), "_route_type": "db_reuse"}, False, True, orig, True)
        logger.debug(
            "[Pipeline] 窗口外重新分析 orig_id=%s reason=%s hash=%s...",
            orig.id,
            "reanalyze_enabled" if policy.reanalyze_after_time_window else "prev_degraded",
            alert_hash[:12],
        )
        res, rean = await analyze_webhook_with_ai(full_data, http_client=http_client), True
    elif check.is_duplicate and orig:
        target = last_beyond if last_beyond and last_beyond.ai_analysis else orig
        if target.ai_analysis and not target.ai_analysis.get("_degraded"):
            logger.debug("[Pipeline] 窗口内复用原始事件分析 orig_id=%s hash=%s...", orig.id, alert_hash[:12])
            await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
            return AnalysisResolution({**target.ai_analysis, "_route_type": "db_reuse"}, False, True, orig, False)
        logger.debug("[Pipeline] 窗口内重新分析 orig_id=%s reason=prev_degraded hash=%s...", orig.id, alert_hash[:12])
        res, rean = await analyze_webhook_with_ai(full_data, http_client=http_client), True
    else:
        logger.debug("[Pipeline] 新事件，发起 AI 分析 hash=%s...", alert_hash[:12])
        res, rean = await analyze_webhook_with_ai(full_data, http_client=http_client), True

    return AnalysisResolution(res, rean, check.is_duplicate, orig, check.beyond_window)
