"""
tests/test_is_retryable.py
==========================
测试 pipeline._is_retryable() 的异常分类逻辑。
该函数决定一个失败的 Webhook 处理是重新入队还是进入死信。
"""

import httpx
import pytest
import sqlalchemy.exc

from services.pipeline import _is_retryable


# ── 不可重试：永久性失败 ──────────────────────────────────────────────────────


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
    """JSON 解析失败是数据问题，重试无意义。"""
    import orjson
    try:
        orjson.loads(b"not-json")
    except orjson.JSONDecodeError as err:
        assert _is_retryable(err) is False
    else:
        pytest.fail("orjson.loads should have raised JSONDecodeError")


def test_value_error_not_retryable():
    assert _is_retryable(ValueError("bad value")) is False


def test_type_error_not_retryable():
    assert _is_retryable(TypeError("wrong type")) is False


def test_key_error_not_retryable():
    assert _is_retryable(KeyError("missing key")) is False


def test_unicode_decode_error_not_retryable():
    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    assert _is_retryable(err) is False


def test_openai_class_name_bad_request_not_retryable():
    """通过类名检测 OpenAI 错误，即使没有实际导入 openai 包。"""
    assert _is_retryable(_FakeBadRequest("prompt too long")) is False


def test_openai_class_name_auth_not_retryable():
    assert _is_retryable(_FakeAuthError("invalid api key")) is False


def test_openai_class_name_permission_not_retryable():
    assert _is_retryable(_FakePermissionDenied("forbidden")) is False


def test_openai_class_name_unprocessable_not_retryable():
    assert _is_retryable(_FakeUnprocessable("entity error")) is False


def test_context_length_message_not_retryable():
    """消息包含 context_length 关键词的任意异常不可重试。"""
    err = RuntimeError("This request exceeds context_length limit")
    assert _is_retryable(err) is False


def test_content_policy_message_not_retryable():
    err = RuntimeError("Blocked by content_policy violation")
    assert _is_retryable(err) is False


def test_content_filter_message_not_retryable():
    err = RuntimeError("content filter triggered")
    assert _is_retryable(err) is False


# ── 可重试：瞬时失败 ──────────────────────────────────────────────────────────


def test_connection_error_retryable():
    """网络断开是瞬时故障，应该重试。"""
    assert _is_retryable(ConnectionError("connection reset")) is True


def test_os_error_retryable():
    assert _is_retryable(OSError("broken pipe")) is True


def test_httpx_timeout_retryable():
    err = httpx.ReadTimeout("timed out", request=None)
    assert _is_retryable(err) is True


def test_httpx_connect_error_retryable():
    err = httpx.ConnectError("failed to connect", request=None)
    assert _is_retryable(err) is True


def test_sqlalchemy_operational_error_retryable():
    """数据库连接中断（如 asyncpg 断线）应该重试。"""
    err = sqlalchemy.exc.OperationalError("stmt", {}, Exception("connection lost"))
    assert _is_retryable(err) is True


def test_generic_runtime_error_retryable():
    """不在黑名单中的通用运行时错误默认可重试。"""
    assert _is_retryable(RuntimeError("unexpected error")) is True


# ── 异常链：__cause__ 链式检查 ────────────────────────────────────────────────


def test_chained_cause_bad_request_not_retryable():
    """外层是通用 RuntimeError，但 __cause__ 是 BadRequest，不可重试。"""
    inner = _FakeBadRequest("model rejected prompt")
    outer = RuntimeError("AI call failed")
    outer.__cause__ = inner
    assert _is_retryable(outer) is False


def test_chained_context_not_retryable():
    """通过 __context__ 链也能检测到不可重试异常。"""
    inner = _FakeAuthError("401")
    outer = Exception("wrapper")
    outer.__context__ = inner
    assert _is_retryable(outer) is False


def test_circular_exception_chain_does_not_hang():
    """循环异常链不能导致无限循环。"""
    err = RuntimeError("cycle")
    err.__cause__ = err  # 循环引用
    # 不抛异常即可
    result = _is_retryable(err)
    assert isinstance(result, bool)
