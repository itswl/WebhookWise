import httpx
import pytest

from core.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException, CircuitState


@pytest.mark.asyncio
async def test_open_circuit_raises_before_call(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = CircuitBreaker("test")
    called = False

    async def blocked_call() -> str:
        nonlocal called
        called = True
        return "ok"

    async def open_state() -> CircuitState:
        return CircuitState.OPEN

    monkeypatch.setattr(breaker, "_check_state_async", open_state)

    with pytest.raises(CircuitBreakerOpenException):
        await breaker.call_async(blocked_call)

    assert called is False


@pytest.mark.asyncio
async def test_expected_exception_is_recorded_and_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = CircuitBreaker("test", expected_exceptions=(httpx.RequestError,))
    recorded = False

    async def closed_state() -> CircuitState:
        return CircuitState.CLOSED

    async def record_failure() -> bool:
        nonlocal recorded
        recorded = True
        return False

    async def failing_call() -> str:
        raise httpx.ConnectError("connect failed")

    monkeypatch.setattr(breaker, "_check_state_async", closed_state)
    monkeypatch.setattr(breaker, "_record_failure", record_failure)

    with pytest.raises(httpx.ConnectError):
        await breaker.call_async(failing_call)

    assert recorded is True


@pytest.mark.asyncio
async def test_success_returns_value_and_records_success(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = CircuitBreaker("test")
    recorded = False

    async def closed_state() -> CircuitState:
        return CircuitState.CLOSED

    async def record_success() -> None:
        nonlocal recorded
        recorded = True

    async def successful_call() -> str:
        return "ok"

    monkeypatch.setattr(breaker, "_check_state_async", closed_state)
    monkeypatch.setattr(breaker, "_record_success", record_success)

    assert await breaker.call_async(successful_call) == "ok"
    assert recorded is True
