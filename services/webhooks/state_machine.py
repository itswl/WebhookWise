from __future__ import annotations

from services.webhooks.types import WebhookProcessingStatus

ALLOWED_TRANSITIONS: dict[WebhookProcessingStatus, set[WebhookProcessingStatus]] = {
    WebhookProcessingStatus.RECEIVED: {WebhookProcessingStatus.ANALYZING},
    WebhookProcessingStatus.RETRY: {WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.RECEIVED},
    WebhookProcessingStatus.FAILED: {WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.RECEIVED},
    WebhookProcessingStatus.ANALYZING: {
        WebhookProcessingStatus.COMPLETED,
        WebhookProcessingStatus.RETRY,
        WebhookProcessingStatus.DEAD_LETTER,
        WebhookProcessingStatus.RECEIVED,
    },
    WebhookProcessingStatus.COMPLETED: set(),
    WebhookProcessingStatus.DEAD_LETTER: {WebhookProcessingStatus.RECEIVED},
}


def allowed_sources(target: WebhookProcessingStatus) -> list[str]:
    return [source.value for source, targets in ALLOWED_TRANSITIONS.items() if target in targets]


def assert_transition_allowed(source: WebhookProcessingStatus, target: WebhookProcessingStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[source]:
        raise ValueError(f"Invalid webhook processing transition: {source.value} -> {target.value}")
