"""
tests/forwarding/test_is_retryable.py
=====================================
Tests the exception-classification logic in retry_policy.should_retry().
This function decides whether a failed webhook processing attempt is re-queued
or sent to the dead-letter queue.
"""

import httpx
import pytest
import sqlalchemy.exc

from core.retry_policies import retry_policy

# ── Not retryable: permanent failures ───────────────────────────────────────────


class _FakeBadRequest(Exception):
    pass


_FakeBadRequest.__name__ = "BadRequestError"


class _FakeAuthError(Exception):
    pass


_FakeAuthError.__name__ = "AuthenticationError"


class _FakePermissionDenied(Exception):
    pass


_FakePermissionDenied.__name__ = "PermissionDeniedError"


class _FakeUnprocessable(Exception):
    pass


_FakeUnprocessable.__name__ = "UnprocessableEntityError"


def test_json_decode_error_not_retryable():
    """A JSON parse failure is a data problem; retrying is pointless."""
    from core import json

    try:
        json.loads(b"not-json")
    except json.JSONDecodeError as err:
        assert retry_policy.should_retry(err) is False
    else:
        pytest.fail("json.loads should have raised JSONDecodeError")


def test_value_error_not_retryable():
    assert retry_policy.should_retry(ValueError("bad value")) is False


def test_type_error_not_retryable():
    assert retry_policy.should_retry(TypeError("wrong type")) is False


def test_key_error_not_retryable():
    assert retry_policy.should_retry(KeyError("missing key")) is False


def test_unicode_decode_error_not_retryable():
    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    assert retry_policy.should_retry(err) is False


def test_openai_class_name_bad_request_not_retryable():
    """Detect OpenAI errors by class name, even without actually importing the openai package."""
    assert retry_policy.should_retry(_FakeBadRequest("prompt too long")) is False


def test_openai_class_name_auth_not_retryable():
    assert retry_policy.should_retry(_FakeAuthError("invalid api key")) is False


def test_openai_class_name_permission_not_retryable():
    assert retry_policy.should_retry(_FakePermissionDenied("forbidden")) is False


def test_openai_class_name_unprocessable_not_retryable():
    assert retry_policy.should_retry(_FakeUnprocessable("entity error")) is False


def test_context_length_message_not_retryable():
    """A generic runtime error is not retryable by default (avoids endless retries that burn tokens)."""
    err = RuntimeError("This request exceeds context_length limit")
    assert retry_policy.should_retry(err) is False


def test_content_policy_message_not_retryable():
    """A generic runtime error is not retryable by default (avoids classifying based on error message text)."""
    err = RuntimeError("Blocked by content_policy violation")
    assert retry_policy.should_retry(err) is False


def test_content_filter_message_not_retryable():
    """A generic runtime error is not retryable by default (avoids classifying based on error message text)."""
    err = RuntimeError("content filter triggered")
    assert retry_policy.should_retry(err) is False


# ── Retryable: transient failures ───────────────────────────────────────────────


def test_connection_error_retryable():
    """A network disconnect is a transient fault and should be retried."""
    assert retry_policy.should_retry(ConnectionError("connection reset")) is True


def test_os_error_retryable():
    assert retry_policy.should_retry(OSError("broken pipe")) is True


def test_httpx_timeout_retryable():
    err = httpx.ReadTimeout("timed out", request=None)
    assert retry_policy.should_retry(err) is True


def test_httpx_connect_error_retryable():
    err = httpx.ConnectError("failed to connect", request=None)
    assert retry_policy.should_retry(err) is True


def test_sqlalchemy_operational_error_retryable():
    """A database connection drop (e.g. an asyncpg disconnect) should be retried."""
    err = sqlalchemy.exc.OperationalError("stmt", {}, Exception("connection lost"))
    assert retry_policy.should_retry(err) is True


def test_generic_runtime_error_retryable():
    """A generic runtime error not in the explicit retryable set is not retryable by default."""
    assert retry_policy.should_retry(RuntimeError("unexpected error")) is False


# ── Exception chains: __cause__ chain inspection ────────────────────────────────


def test_chained_cause_bad_request_not_retryable():
    """The outer error is a generic RuntimeError, but __cause__ is a BadRequest: not retryable."""
    inner = _FakeBadRequest("model rejected prompt")
    outer = RuntimeError("AI call failed")
    outer.__cause__ = inner
    assert retry_policy.should_retry(outer) is False


def test_chained_context_not_retryable():
    """A non-retryable exception can also be detected through the __context__ chain."""
    inner = _FakeAuthError("401")
    outer = Exception("wrapper")
    outer.__context__ = inner
    assert retry_policy.should_retry(outer) is False


def test_circular_exception_chain_does_not_hang():
    """A circular exception chain must not cause an infinite loop."""
    err = RuntimeError("cycle")
    err.__cause__ = err  # circular reference
    # Not raising an exception is sufficient
    result = retry_policy.should_retry(err)
    assert isinstance(result, bool)
