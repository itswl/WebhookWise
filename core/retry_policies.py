"""Retry classification policies for infrastructure failures."""

from typing import Any

import httpx
import sqlalchemy.exc
from asyncpg.exceptions import QueryCanceledError
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from redis.exceptions import RedisError

from core import json

_NON_RETRYABLE_ERRORS = (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError)
_OPENAI_NON_RETRYABLE_ERRORS = (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    UnprocessableEntityError,
    NotFoundError,
)
_OPENAI_RETRYABLE_ERRORS = (RateLimitError, APIConnectionError, APITimeoutError)
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
            if isinstance(curr, QueryCanceledError):
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

            if isinstance(curr, APIStatusError):
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
        return isinstance(exc, _OPENAI_NON_RETRYABLE_ERRORS) or type(exc).__name__ in _OPENAI_NON_RETRYABLE_CLASS_NAMES

    def _is_openai_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, _OPENAI_RETRYABLE_ERRORS) or type(exc).__name__ in _OPENAI_RETRYABLE_CLASS_NAMES

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
