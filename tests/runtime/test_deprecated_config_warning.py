"""The deprecation nudge fires for configurations that will actually break."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core.service_lifecycle import _warn_deprecated_config

_MESSAGE_MARKER = "PROCESSING_LOCK_FAILFAST_* is deprecated"


def _config(*, explicit: set[str], canonical_threshold: int) -> SimpleNamespace:
    return SimpleNamespace(
        retry=SimpleNamespace(
            PROCESSING_LOCK_FAILFAST_THRESHOLD=20,
            PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS=10,
            model_fields_set=explicit,
        ),
        mq=SimpleNamespace(WEBHOOK_INGRESS_STORM_THRESHOLD=canonical_threshold),
    )


def test_warns_when_legacy_keys_are_explicit_and_canonical_unset(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_deprecated_config(_config(explicit={"PROCESSING_LOCK_FAILFAST_THRESHOLD"}, canonical_threshold=0))
    assert _MESSAGE_MARKER in caplog.text


def test_silent_on_code_defaults(caplog: pytest.LogCaptureFixture) -> None:
    """A deployment that never wrote these keys gets no unactionable warning."""
    with caplog.at_level(logging.WARNING):
        _warn_deprecated_config(_config(explicit=set(), canonical_threshold=0))
    assert _MESSAGE_MARKER not in caplog.text


def test_silent_once_canonical_keys_are_set(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_deprecated_config(_config(explicit={"PROCESSING_LOCK_FAILFAST_THRESHOLD"}, canonical_threshold=50))
    assert _MESSAGE_MARKER not in caplog.text


def test_partial_config_double_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_deprecated_config(SimpleNamespace())
    assert _MESSAGE_MARKER not in caplog.text
