"""Deep-analysis completion notification side effects."""

import logging

from core.logger import mask_url
from services.analysis.openclaw_poll_policy import OpenClawPollPolicy
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.deep_analysis_notifications")


async def send_deep_analysis_success_notification(
    record_dict: WebhookData,
    source: str = "",
    *,
    policy: OpenClawPollPolicy | None = None,
) -> None:
    """Send a configured notification for completed deep analysis."""
    policy = policy or OpenClawPollPolicy.from_config()
    webhook_url = policy.notification_webhook_url
    if not webhook_url:
        return

    from services.operations.feishu_notifications import send_feishu_deep_analysis

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
    except Exception as e:
        logger.warning("深度分析通知失败: %s", e)


async def send_deep_analysis_failure_notification(
    record_dict: WebhookData,
    reason: str = "",
    *,
    policy: OpenClawPollPolicy | None = None,
) -> None:
    """Send a configured notification for failed deep analysis."""
    policy = policy or OpenClawPollPolicy.from_config()
    webhook_url = policy.notification_webhook_url
    if not webhook_url:
        return

    from services.operations.feishu_notifications import send_feishu_deep_analysis

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
    except Exception as e:
        logger.warning("[DeepAnalysisNotify] 失败通知异常: %s", e)


async def _record_notification_failure(
    webhook_event_id: int,
    target_url: str,
    *,
    failure_reason: str,
    error_message: str,
    analysis_type: str,
) -> None:
    try:
        from services.forwarding.forward import record_failed_forward

        await record_failed_forward(
            webhook_event_id=webhook_event_id,
            forward_rule_id=None,
            target_url=target_url,
            target_type="feishu",
            failure_reason=failure_reason,
            error_message=error_message,
            forward_data={"webhook_event_id": webhook_event_id, "analysis_type": analysis_type},
        )
    except Exception as rec_err:
        logger.warning("记录深度分析通知失败异常: %s", rec_err)
