import pytest

from services.webhooks.state_machine import allowed_sources, assert_transition_allowed
from services.webhooks.types import WebhookProcessingStatus


def test_analyzing_sources_are_explicit() -> None:
    assert allowed_sources(WebhookProcessingStatus.ANALYZING) == ["received", "retry", "failed"]


def test_invalid_terminal_transition_is_rejected() -> None:
    with pytest.raises(ValueError):
        assert_transition_allowed(WebhookProcessingStatus.COMPLETED, WebhookProcessingStatus.RETRY)
