import pytest

from services.webhooks.state_machine import ALLOWED_TRANSITIONS, allowed_sources, assert_transition_allowed
from services.webhooks.types import WebhookProcessingStatus


class TestAllowedSources:
    def test_analyzing_sources(self) -> None:
        assert set(allowed_sources(WebhookProcessingStatus.ANALYZING)) == {
            "received",
            "retry",
            "failed",
        }

    def test_received_sources(self) -> None:
        assert set(allowed_sources(WebhookProcessingStatus.RECEIVED)) == {
            "retry",
            "failed",
            "analyzing",
            "dead_letter",
        }

    def test_completed_sources(self) -> None:
        assert allowed_sources(WebhookProcessingStatus.COMPLETED) == ["analyzing"]

    def test_dead_letter_sources(self) -> None:
        assert allowed_sources(WebhookProcessingStatus.DEAD_LETTER) == ["analyzing"]

    def test_retry_sources(self) -> None:
        assert set(allowed_sources(WebhookProcessingStatus.RETRY)) == {"analyzing"}

    def test_failed_sources(self) -> None:
        assert allowed_sources(WebhookProcessingStatus.FAILED) == []


class TestAssertTransitionAllowed:
    # ── valid transitions ────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.RETRY, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.RETRY, WebhookProcessingStatus.RECEIVED),
            (WebhookProcessingStatus.FAILED, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.FAILED, WebhookProcessingStatus.RECEIVED),
            (WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.COMPLETED),
            (WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.RETRY),
            (WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.DEAD_LETTER),
            (WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.RECEIVED),
            (WebhookProcessingStatus.DEAD_LETTER, WebhookProcessingStatus.RECEIVED),
        ],
    )
    def test_valid_transition(self, source: WebhookProcessingStatus, target: WebhookProcessingStatus) -> None:
        assert_transition_allowed(source, target)

    # ── invalid transitions ──────────────────────────────────────────

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            # terminal states cannot transition to anything
            (WebhookProcessingStatus.COMPLETED, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.COMPLETED, WebhookProcessingStatus.RETRY),
            (WebhookProcessingStatus.COMPLETED, WebhookProcessingStatus.RECEIVED),
            # cannot go directly to terminal from entry
            (WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.COMPLETED),
            (WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.DEAD_LETTER),
            (WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.RETRY),
            # dead_letter can only go to received
            (WebhookProcessingStatus.DEAD_LETTER, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.DEAD_LETTER, WebhookProcessingStatus.COMPLETED),
            # self-transitions are not allowed
            (WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.RECEIVED),
            (WebhookProcessingStatus.ANALYZING, WebhookProcessingStatus.ANALYZING),
            (WebhookProcessingStatus.RETRY, WebhookProcessingStatus.RETRY),
        ],
    )
    def test_invalid_transition_raises(self, source: WebhookProcessingStatus, target: WebhookProcessingStatus) -> None:
        with pytest.raises(ValueError, match="Invalid webhook processing transition"):
            assert_transition_allowed(source, target)


class TestTransitionGraphCompleteness:
    def test_every_status_has_an_entry(self) -> None:
        all_statuses = set(WebhookProcessingStatus)
        assert set(ALLOWED_TRANSITIONS.keys()) == all_statuses

    def test_all_targets_are_valid_statuses(self) -> None:
        all_statuses = set(WebhookProcessingStatus)
        for targets in ALLOWED_TRANSITIONS.values():
            assert targets <= all_statuses
