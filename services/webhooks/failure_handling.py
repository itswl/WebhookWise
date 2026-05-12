"""Webhook processing failure handling and retry state transitions."""

from collections.abc import Awaitable, Callable
from typing import Any

from core.logger import logger
from core.metrics import WEBHOOK_DEAD_LETTER_TOTAL, WEBHOOK_PROCESSING_STATUS_TOTAL
from core.retry_policies import retry_policy
from services.operations.dead_letter_notifications import notify_dead_letter
from services.operations.taskiq_retry_scheduler import compute_backoff_delay, schedule_webhook_retry
from services.webhooks.policies import WebhookFailurePolicy
from services.webhooks.repository import mark_dead_letter, mark_retry
from services.webhooks.types import WebhookProcessingStatus

DeadLetterNotifier = Callable[[int, int, Exception], Awaitable[None]]
RetryClassifier = Callable[[Exception], bool]
RetryScheduler = Callable[[int, int], Awaitable[None]]


def set_span_error(span: Any, message: str) -> None:
    if not span:
        return
    try:
        from opentelemetry.trace import StatusCode

        span.set_status(StatusCode.ERROR, message)
    except Exception:
        return


async def handle_process_exception(
    event_id: int,
    err: Exception,
    span: Any | None,
    *,
    policy: WebhookFailurePolicy | None = None,
    retry_classifier: RetryClassifier = retry_policy.should_retry,
    retry_scheduler: RetryScheduler = schedule_webhook_retry,
    dead_letter_notifier: DeadLetterNotifier | None = None,
) -> str:
    policy = policy or WebhookFailurePolicy.from_config()
    dead_letter_notifier = dead_letter_notifier or notify_dead_letter
    retryable = retry_classifier(err)
    max_retries = policy.max_retries
    next_retry_count = 0
    status = WebhookProcessingStatus.DEAD_LETTER
    if retryable:
        marked_retry = await mark_retry(
            event_id,
            max_retries=max_retries,
            error_message=str(err),
            initial_delay=policy.initial_delay,
            max_delay=policy.max_delay,
            multiplier=policy.backoff_multiplier,
        )
        retry_delay: int | None = None
        if marked_retry is not None:
            next_retry_count, retry_delay = marked_retry
            status = WebhookProcessingStatus.RETRY

        if status == WebhookProcessingStatus.RETRY:
            delay = int(
                retry_delay
                or compute_backoff_delay(
                    next_retry_count,
                    initial_delay=policy.initial_delay,
                    max_delay=policy.max_delay,
                    multiplier=policy.backoff_multiplier,
                )
            )
            try:
                await retry_scheduler(event_id, delay)
            except Exception as enqueue_err:
                fatal_error = (
                    "retry enqueue failed after retryable processing error: "
                    f"process_error={err}; enqueue_error={enqueue_err}"
                )
                await mark_dead_letter(event_id, retryable=True, error_message=fatal_error)
                await dead_letter_notifier(event_id, next_retry_count, enqueue_err)
                WEBHOOK_DEAD_LETTER_TOTAL.inc()
                outcome = "dead_letter"
                WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
                logger.critical(
                    "[WebhookFailure] TaskIQ 延迟重试调度失败，事件直接进入 dead-letter event_id=%s retry=%s/%s error=%s",
                    event_id,
                    next_retry_count,
                    max_retries,
                    enqueue_err,
                    exc_info=True,
                )
                if span:
                    set_span_error(span, fatal_error)
                return outcome

            outcome = "retry"
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
            logger.error(
                "[WebhookFailure] 处理失败，已进入 TaskIQ 延迟重试 event_id=%s retry=%s/%s delay=%ss error=%s",
                event_id,
                next_retry_count,
                max_retries,
                delay,
                err,
                exc_info=True,
            )
            if span:
                set_span_error(span, str(err))
            return outcome

    await mark_dead_letter(event_id, retryable=retryable, error_message=str(err))
    await dead_letter_notifier(event_id, next_retry_count, err)
    WEBHOOK_DEAD_LETTER_TOTAL.inc()
    outcome = "dead_letter"
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
    logger.error("[WebhookFailure] 处理失败 event_id=%s retryable=%s error=%s", event_id, retryable, err, exc_info=True)
    set_span_error(span, str(err))
    return outcome
