import httpx

from services import pipeline


def test_is_retryable_http_timeout_is_retryable():
    exc = httpx.TimeoutException("timeout")
    assert pipeline._is_retryable(exc) is True


def test_is_retryable_openai_bad_request_is_not_retryable():
    class BadRequestError(Exception):
        pass

    assert pipeline._is_retryable(BadRequestError("maximum context length")) is False


def test_is_retryable_openai_bad_request_in_exception_chain_is_not_retryable():
    class BadRequestError(Exception):
        pass

    try:
        try:
            raise BadRequestError("content policy violation")
        except BadRequestError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert pipeline._is_retryable(outer) is False
