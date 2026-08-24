"""Which provider failures earn a retry, and the one that was silently not.

A rate limit is retryable everywhere in this codebase — 429 is in the status set
`is_ai_provider_retryable_error` checks. It still went terminal, because the
exception that actually reached the classifier was not the provider's.
"""

from __future__ import annotations

import httpx
import pytest

from services.analysis.ai_errors import is_ai_provider_retryable_error, iter_exception_chain


class _Rated(Exception):
    status_code = 429


class _Refused(Exception):
    status_code = 400


def _instructor_exception(*inner: Exception) -> Exception:
    from instructor.core.exceptions import FailedAttempt, InstructorRetryException

    return InstructorRetryException(
        "gave up",
        n_attempts=len(inner),
        total_usage=0,
        failed_attempts=[FailedAttempt(n + 1, exc, None) for n, exc in enumerate(inner)],
    )


def test_a_rate_limit_wrapped_by_instructor_is_still_a_rate_limit() -> None:
    """The regression this fixes.

    instructor carries the real errors in `failed_attempts` and chains none of
    them, so the exception arriving at the classifier had no status_code, no
    "rate" in its type name and an empty __cause__/__context__ chain. It was
    read as terminal, the analysis fell back to rules, and the only trace was a
    log line. Measured live against a rate-limited account: six of eighteen eval
    cases died this way, and one of them silently became an unscored case in a
    baseline that is supposed to gate prompt changes.
    """
    assert is_ai_provider_retryable_error(_instructor_exception(_Rated("429"))) is True


def test_a_wrapped_refusal_is_still_terminal() -> None:
    """The other direction has to hold or the fix is just "retry everything",
    which turns a bad request into a paid loop."""
    assert is_ai_provider_retryable_error(_instructor_exception(_Refused("400"))) is False


def test_the_chain_reaches_every_failed_attempt() -> None:
    """A wrapper may hold several attempts and only one of them may be the
    retryable kind; finding it must not depend on which came first."""
    wrapped = _instructor_exception(_Refused("400"), _Rated("429"))
    chain = iter_exception_chain(wrapped)

    assert any(isinstance(e, _Rated) for e in chain)
    assert any(isinstance(e, _Refused) for e in chain)
    assert is_ai_provider_retryable_error(wrapped) is True


def test_a_cycle_in_the_chain_terminates() -> None:
    """Walking two directions at once (chain plus attempts) invites a loop; the
    visited set is what stops it, and a test is what proves the set is used."""
    first = _Rated("429")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    chain = iter_exception_chain(_instructor_exception(first))

    assert len(chain) == 3, chain


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
    ],
)
def test_the_transport_failures_stay_retryable(exc: Exception) -> None:
    """Nothing above should have narrowed what already worked."""
    assert is_ai_provider_retryable_error(exc) is True
