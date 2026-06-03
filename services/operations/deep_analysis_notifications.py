"""Deep-analysis completion notification side effects."""

from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from contracts.webhook_payload import JsonObject
from core.logger import get_logger, mask_url
from services.forwarding.outbox import forward_notification
from services.notifications.feishu import build_deep_analysis_card

if TYPE_CHECKING:
    from services.analysis.openclaw_client import OpenClawPollPolicy

logger = get_logger("deep_analysis_notifications")

EVENT_IMPORTANCE_KEY = "_event_importance"
EVENT_IS_DUPLICATE_KEY = "_event_is_duplicate"
EVENT_PARSED_DATA_KEY = "_event_parsed_data"
_NOTIFICATION_ERRORS = (KeyError, OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError)


async def send_feishu_deep_analysis(
    webhook_url: str,
    analysis_record: dict[str, Any],
    source: str = "",
    webhook_event_id: int = 0,
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> bool:
    if not webhook_url:
        return False
    try:
        payload = build_deep_analysis_card(analysis_record, source=source, webhook_event_id=webhook_event_id)
        await forward_notification(
            event_type="deep_analysis",
            source=source,
            formatted_payload=payload,
            webhook_id=webhook_event_id or None,
            importance=importance,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
        )
        return True
    except _NOTIFICATION_ERRORS as e:
        logger.warning("深度分析通知入队失败: %s", e)
        return False


async def send_deep_analysis_success_notification(
    record_dict: JsonObject,
    source: str = "",
    *,
    policy: "OpenClawPollPolicy | None" = None,
) -> None:
    """Send a configured notification for completed deep analysis."""
    from services.analysis.openclaw_client import OpenClawPollPolicy

    policy = policy or OpenClawPollPolicy.from_config()
    webhook_url = policy.notification_webhook_url
    if not webhook_url:
        return

    try:
        event_id = int(record_dict["webhook_event_id"])
        logger.info(
            "[DeepAnalysisNotify] 准备发送成功通知 id=%s event_id=%s target=%s",
            record_dict.get("id"),
            event_id,
            mask_url(webhook_url),
        )
        analysis_data = {
            "analysis_result": record_dict["analysis_result"],
            "engine": record_dict["engine"],
            "duration_seconds": record_dict.get("duration_seconds") or 0,
        }
        success = await send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record=analysis_data,
            source=source,
            webhook_event_id=event_id,
            importance=str(record_dict.get(EVENT_IMPORTANCE_KEY) or ""),
            is_duplicate=bool(record_dict.get(EVENT_IS_DUPLICATE_KEY)),
            parsed_data=record_dict.get(EVENT_PARSED_DATA_KEY)
            if isinstance(record_dict.get(EVENT_PARSED_DATA_KEY), dict)
            else None,
        )
        if success:
            logger.info(
                "[DeepAnalysisNotify] 通知已发送: id=%s event_id=%s",
                record_dict.get("id"),
                event_id,
            )
            return

        await _record_notification_failure(
            event_id,
            webhook_url,
            failure_reason="feishu_notification_failed",
            error_message="深度分析飞书通知发送失败",
            analysis_type="deep_analysis",
        )
        logger.warning(
            "[DeepAnalysisNotify] 成功通知发送失败 id=%s event_id=%s target=%s",
            record_dict.get("id"),
            event_id,
            mask_url(webhook_url),
        )
    except _NOTIFICATION_ERRORS as e:
        logger.warning("深度分析通知失败: %s", e)


async def send_deep_analysis_failure_notification(
    record_dict: JsonObject,
    reason: str = "",
    *,
    policy: "OpenClawPollPolicy | None" = None,
) -> None:
    """Send a configured notification for failed deep analysis."""
    from services.analysis.openclaw_client import OpenClawPollPolicy

    policy = policy or OpenClawPollPolicy.from_config()
    webhook_url = policy.notification_webhook_url
    if not webhook_url:
        return

    try:
        event_id = int(record_dict["webhook_event_id"])
        logger.info(
            "[DeepAnalysisNotify] 准备发送失败通知 id=%s event_id=%s target=%s",
            record_dict.get("id"),
            event_id,
            mask_url(webhook_url),
        )
        analysis_result = record_dict.get("analysis_result")
        failed_result = dict(analysis_result) if isinstance(analysis_result, dict) else {}
        failed_result["analysis_failed"] = True
        failed_result["failure_reason"] = reason

        success = await send_feishu_deep_analysis(
            webhook_url=webhook_url,
            analysis_record={
                "analysis_result": failed_result,
                "engine": record_dict["engine"],
                "duration_seconds": record_dict.get("duration_seconds") or 0,
            },
            source="",
            webhook_event_id=event_id,
            importance=str(record_dict.get(EVENT_IMPORTANCE_KEY) or ""),
            is_duplicate=bool(record_dict.get(EVENT_IS_DUPLICATE_KEY)),
            parsed_data=record_dict.get(EVENT_PARSED_DATA_KEY)
            if isinstance(record_dict.get(EVENT_PARSED_DATA_KEY), dict)
            else None,
        )
        if success:
            logger.info("[DeepAnalysisNotify] 失败通知已发送: id=%s reason=%s", record_dict["id"], reason)
            return

        await _record_notification_failure(
            event_id,
            webhook_url,
            failure_reason="feishu_failure_notification_failed",
            error_message=f"深度分析失败飞书通知发送失败: {reason}",
            analysis_type="deep_analysis_failed",
        )
        logger.warning(
            "[DeepAnalysisNotify] 失败通知发送失败 id=%s event_id=%s target=%s",
            record_dict.get("id"),
            event_id,
            mask_url(webhook_url),
        )
    except _NOTIFICATION_ERRORS as e:
        logger.warning("[DeepAnalysisNotify] 失败通知异常: %s", e)


async def _record_notification_failure(
    webhook_event_id: int,
    target_url: str,
    *,
    failure_reason: str,
    error_message: str,
    analysis_type: str,
) -> None:
    logger.warning(
        "[DeepAnalysisNotify] 通知失败 event_id=%s target=%s reason=%s error=%s analysis_type=%s",
        webhook_event_id,
        mask_url(target_url),
        failure_reason,
        error_message,
        analysis_type,
    )
