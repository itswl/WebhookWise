"""AI provider exception classification helpers."""

from __future__ import annotations

import httpx

_AI_POLICY_REFUSAL_MARKERS = (
    "terms of service",
    "content_policy",
    "content policy",
    "content filter",
    "prohibited",
    "policy violation",
    "violation of provider",
)

_AI_PROVIDER_MODULE_PREFIXES = (
    "aiohttp",
    "httpcore",
    "httpx",
    "instructor",
    "openai",
)

_AI_PROVIDER_RUNTIME_TYPES = (
    ConnectionError,
    OSError,
    TimeoutError,
    httpx.RequestError,
    httpx.TimeoutException,
)


def iter_exception_chain(root: BaseException) -> list[BaseException]:
    visited: set[int] = set()
    out: list[BaseException] = []
    curr: BaseException | None = root
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        out.append(curr)
        curr = curr.__cause__ or curr.__context__
    return out


def extract_ai_error_message(exc: BaseException) -> str:
    for curr in iter_exception_chain(exc):
        body = getattr(curr, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = err.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
        text = str(curr).strip()
        if text:
            return text[:500]
    return type(exc).__name__


def is_ai_policy_refusal(exc: BaseException) -> bool:
    for curr in iter_exception_chain(exc):
        error_text = str(curr).lower()
        body = getattr(curr, "body", None)
        if isinstance(body, dict):
            error_text += f" {body!s}".lower()

        if any(marker in error_text for marker in _AI_POLICY_REFUSAL_MARKERS):
            return True

        status_code = getattr(curr, "status_code", None)
        if type(curr).__name__ == "PermissionDeniedError" and status_code == 403:
            return True

    return False


def is_ai_provider_runtime_error(exc: BaseException) -> bool:
    """Return true for failures that plausibly came from the AI provider stack.

    The module-name and HTTP-shape checks keep this resilient when SDKs add new
    exception classes while still letting unrelated application bugs propagate.
    """
    for curr in iter_exception_chain(exc):
        if isinstance(curr, _AI_PROVIDER_RUNTIME_TYPES):
            return True

        module_name = type(curr).__module__
        if module_name.startswith(_AI_PROVIDER_MODULE_PREFIXES):
            return True

        status_code = getattr(curr, "status_code", None)
        if isinstance(status_code, int) and type(curr).__name__.endswith("Error"):
            return True

    return False


def is_ai_provider_retryable_error(exc: BaseException) -> bool:
    for curr in iter_exception_chain(exc):
        if isinstance(curr, (httpx.RequestError, httpx.TimeoutException, ConnectionError, TimeoutError)):
            return True

        status_code = getattr(curr, "status_code", None)
        if isinstance(status_code, int) and (status_code in {408, 409, 425, 429} or status_code >= 500):
            return True

        name = type(curr).__name__.lower()
        module_name = type(curr).__module__
        if module_name.startswith(_AI_PROVIDER_MODULE_PREFIXES) and (
            "connection" in name or "timeout" in name or "rate" in name
        ):
            return True

    return False
