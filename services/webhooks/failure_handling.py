"""Webhook processing failure handling and retry state transitions."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.logger import logger
from core.observability.metrics import WEBHOOK_DEAD_LETTER_TOTAL, WEBHOOK_PROCESSING_STATUS_TOTAL
from core.observability.tracing import set_span_error
from core.retry_policies import retry_policy
from services.operations.dead_letter_notifications import notify_dead_letter
from services.operations.taskiq_retry_scheduler import compute_backoff_delay, schedule_webhook_retry
from services.webhooks.policies import WebhookFailurePolicy
from services.webhooks.repository import mark_dead_letter, mark_retry
from services.webhooks.types import WebhookProcessingStatus

DeadLetterNotifier = Callable[[int, int, Exception], Awaitable[None]]
RetryClassifier = Callable[[Exception], bool]
RetryScheduler = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _RetryAttempt:
    count: int
    delay: int


def _record_processing_outcome(outcome: str) -> str:
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status=outcome).inc()
    return outcome


async def _mark_retry_if_available(event_id: int, err: Exception, policy: WebhookFailurePolicy) -> _RetryAttempt | None:
    marked_retry = await mark_retry(
        event_id,
        max_retries=policy.max_retries,
        error_message=str(err),
        initial_delay=policy.initial_delay,
        max_delay=policy.max_delay,
        multiplier=policy.backoff_multiplier,
    )
    if marked_retry is None:
        return None

    retry_count, retry_delay = marked_retry
    delay = int(
        retry_delay
        or compute_backoff_delay(
            retry_count,
            initial_delay=policy.initial_delay,
            max_delay=policy.max_delay,
            multiplier=policy.backoff_multiplier,
        )
    )
    return _RetryAttempt(count=retry_count, delay=delay)


async def _move_to_dead_letter(
    event_id: int,
    err: Exception,
    *,
    retryable: bool,
    retry_count: int,
    dead_letter_notifier: DeadLetterNotifier,
    span: Any | None,
    error_message: str | None = None,
    log_level: str = "error",
) -> str:
    persisted_error = error_message or str(err)
    await mark_dead_letter(event_id, retryable=retryable, error_message=persisted_error)
    await dead_letter_notifier(event_id, retry_count, err)
    WEBHOOK_DEAD_LETTER_TOTAL.inc()
    outcome = _record_processing_outcome("dead_letter")
    log = logger.critical if log_level == "critical" else logger.error
    log("[WebhookFailure] 处理失败 event_id=%s retryable=%s error=%s", event_id, retryable, err, exc_info=True)
    set_span_error(span, persisted_error)
    return outcome


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

    if retryable:
        retry_attempt = await _mark_retry_if_available(event_id, err, policy)
        if retry_attempt is not None:
            try:
                await retry_scheduler(event_id, retry_attempt.delay)
            except Exception as enqueue_err:
                fatal_error = (
                    "retry enqueue failed after retryable processing error: "
                    f"process_error={err}; enqueue_error={enqueue_err}"
                )
                logger.critical(
                    "[WebhookFailure] TaskIQ 延迟重试调度失败，事件直接进入 dead-letter event_id=%s retry=%s/%s error=%s",
                    event_id,
                    retry_attempt.count,
                    policy.max_retries,
                    enqueue_err,
                    exc_info=True,
                )
                return await _move_to_dead_letter(
                    event_id,
                    enqueue_err,
                    retryable=True,
                    retry_count=retry_attempt.count,
                    dead_letter_notifier=dead_letter_notifier,
                    span=span,
                    error_message=fatal_error,
                    log_level="critical",
                )

            outcome = _record_processing_outcome(WebhookProcessingStatus.RETRY.value)
            logger.error(
                "[WebhookFailure] 处理失败，已进入 TaskIQ 延迟重试 event_id=%s retry=%s/%s delay=%ss error=%s",
                event_id,
                retry_attempt.count,
                policy.max_retries,
                retry_attempt.delay,
                err,
                exc_info=True,
            )
            set_span_error(span, str(err))
            return outcome

    return await _move_to_dead_letter(
        event_id,
        err,
        retryable=retryable,
        retry_count=0,
        dead_letter_notifier=dead_letter_notifier,
        span=span,
    )
