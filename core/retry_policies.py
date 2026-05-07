"""Retry classification policies for infrastructure failures."""

from typing import Any, cast

import httpx
import orjson
import sqlalchemy.exc
from redis.exceptions import RedisError

_QueryCanceledError: type[BaseException] | None = None
try:
    from asyncpg.exceptions import QueryCanceledError

    _QueryCanceledError = QueryCanceledError
except ImportError:
    pass

_OpenAIAuthenticationError: type[Exception] | None = None
_OpenAIBadRequestError: type[Exception] | None = None
_OpenAIPermissionDeniedError: type[Exception] | None = None
_OpenAIUnprocessableEntityError: type[Exception] | None = None
_OpenAINotFoundError: type[Exception] | None = None
_OpenAIRateLimitError: type[Exception] | None = None
_OpenAIAPIConnectionError: type[Exception] | None = None
_OpenAIAPITimeoutError: type[Exception] | None = None
_OpenAIAPIStatusError: type[Exception] | None = None

try:
    from openai import AuthenticationError, BadRequestError, PermissionDeniedError, UnprocessableEntityError

    _OpenAIAuthenticationError = AuthenticationError
    _OpenAIBadRequestError = BadRequestError
    _OpenAIPermissionDeniedError = PermissionDeniedError
    _OpenAIUnprocessableEntityError = UnprocessableEntityError
    try:
        from openai import NotFoundError, RateLimitError

        _OpenAINotFoundError = NotFoundError
        _OpenAIRateLimitError = RateLimitError
    except ImportError:
        pass

    try:
        from openai import APIConnectionError, APITimeoutError

        _OpenAIAPIConnectionError = APIConnectionError
        _OpenAIAPITimeoutError = APITimeoutError
    except ImportError:
        pass

    try:
        from openai import APIStatusError

        _OpenAIAPIStatusError = APIStatusError
    except ImportError:
        pass
except ImportError:
    pass

_NON_RETRYABLE_ERRORS = (ValueError, KeyError, TypeError, orjson.JSONDecodeError, UnicodeDecodeError)
_OPENAI_NON_RETRYABLE_CLASS_NAMES = {
    "AuthenticationError",
    "BadRequestError",
    "PermissionDeniedError",
    "UnprocessableEntityError",
    "NotFoundError",
}
_OPENAI_RETRYABLE_CLASS_NAMES = {"RateLimitError", "APIConnectionError", "APITimeoutError"}


class RetryPolicy:
    """Classifies exceptions into retryable and terminal failures."""

    def should_retry(self, exc: Exception) -> bool:
        for curr in self._iter_chain(exc):
            if isinstance(curr, _NON_RETRYABLE_ERRORS):
                return False
            if _QueryCanceledError and isinstance(curr, _QueryCanceledError):
                return True
            if isinstance(curr, RedisError):
                return True
            if isinstance(
                curr, (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError, ConnectionError, OSError)
            ):
                return True
            if isinstance(curr, sqlalchemy.exc.OperationalError):
                return True

            if self._is_openai_non_retryable(curr):
                return False
            if self._is_openai_retryable(curr):
                return True

            if _OpenAIAPIStatusError and isinstance(curr, self._as_tuple(_OpenAIAPIStatusError)):
                status = self._extract_status_code(curr)
                if status is not None:
                    return self._should_retry_http_status(status)

            status = self._extract_status_code(curr)
            if status is not None:
                return self._should_retry_http_status(status)

            code = self._openai_error_code(curr)
            if code in {"context_length_exceeded", "content_policy_violation"}:
                return False

        return False

    def _is_openai_non_retryable(self, exc: BaseException) -> bool:
        openai_non_retryable = self._as_tuple(
            _OpenAIAuthenticationError,
            _OpenAIBadRequestError,
            _OpenAIPermissionDeniedError,
            _OpenAIUnprocessableEntityError,
            _OpenAINotFoundError,
        )
        return isinstance(exc, openai_non_retryable) or type(exc).__name__ in _OPENAI_NON_RETRYABLE_CLASS_NAMES

    def _is_openai_retryable(self, exc: BaseException) -> bool:
        openai_retryable = self._as_tuple(_OpenAIRateLimitError, _OpenAIAPIConnectionError, _OpenAIAPITimeoutError)
        return isinstance(exc, openai_retryable) or type(exc).__name__ in _OPENAI_RETRYABLE_CLASS_NAMES

    @staticmethod
    def _iter_chain(root: BaseException) -> list[BaseException]:
        visited: set[int] = set()
        out: list[BaseException] = []
        curr: BaseException | None = root
        while curr is not None and id(curr) not in visited:
            visited.add(id(curr))
            out.append(curr)
            curr = curr.__cause__ or curr.__context__
        return out

    @staticmethod
    def _as_tuple(*items: object) -> tuple[type[BaseException], ...]:
        return tuple(cast(type[BaseException], item) for item in items if isinstance(item, type))

    @staticmethod
    def _extract_status_code(exc: BaseException) -> int | None:
        if isinstance(exc, httpx.HTTPStatusError):
            return int(getattr(exc.response, "status_code", 0) or 0) or None
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status > 0:
            return status
        response = getattr(exc, "response", None)
        if response is not None:
            resp_status = getattr(response, "status_code", None)
            if isinstance(resp_status, int) and resp_status > 0:
                return resp_status
        return None

    @staticmethod
    def _should_retry_http_status(status_code: int) -> bool:
        if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        if 400 <= status_code < 500:
            return False
        return 500 <= status_code < 600

    @staticmethod
    def _openai_error_code(exc: BaseException) -> str | None:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.strip():
            return code.strip()
        body: Any = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                nested_code = err.get("code")
                if isinstance(nested_code, str) and nested_code.strip():
                    return nested_code.strip()
        return None


retry_policy = RetryPolicy()
