"""Integration tests for CircuitBreaker — state-machine level tests.

Since fakeredis doesn't support Lua EVAL, we mock the two redis helpers
(redis_eval_int, redis_eval_str) with in-memory dicts that faithfully
reproduce the Lua script semantics. This tests the CircuitBreaker logic
end-to-end without tautological method-level monkeypatching.
"""

import time
from unittest.mock import patch

import pytest

from core.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException, CircuitState


class _FakeRedisState:
    """Minimal in-memory Redis that simulates the circuit-breaker Lua scripts."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.expiry: dict[str, float] = {}

    def _is_expired(self, key: str) -> bool:
        exp = self.expiry.get(key)
        return exp is not None and time.time() > exp

    def get(self, key: str) -> str | None:
        if self._is_expired(key):
            return None
        return self.data.get(key)

    def set_kv(self, key: str, value: str) -> None:
        self.data[key] = value

    def set_expire(self, key: str, ttl: int) -> None:
        self.expiry[key] = time.time() + ttl

    def incr(self, key: str) -> int:
        val = int(self.data.get(key, "0"))
        val += 1
        self.data[key] = str(val)
        return val

    def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self.expiry.pop(key, None)


def _make_eval_helpers(state: _FakeRedisState):
    """Return (eval_int, eval_str) callables matching core.redis_client signatures."""

    async def _eval_int(script: str, numkeys: int, *args: object) -> int:
        keys = [str(a) for a in args[:numkeys]]
        argv = [str(a) for a in args[numkeys:]]
        # Dispatch based on which Lua script is being called
        if "incr" in script and "failures" in script.lower() or "failures_key" in script:
            # _CB_RECORD_FAILURE_LUA
            failures_key, state_key, open_until_key = keys[0], keys[1], keys[2]
            failure_window, threshold, open_until_ts, state_expire = argv[0], argv[1], argv[2], argv[3]
            failures = state.incr(failures_key)
            if failures == 1:
                state.set_expire(failures_key, int(failure_window))
            if failures >= int(threshold):
                state.set_kv(state_key, "open")
                state.set_kv(open_until_key, open_until_ts)
                state.set_expire(state_key, int(state_expire))
                state.set_expire(open_until_key, int(state_expire))
                return 1
            return 0
        elif "half_open" in script:
            # _CB_RECORD_SUCCESS_LUA
            failures_key, state_key, open_until_key = keys[0], keys[1], keys[2]
            current_state = state.get(state_key)
            if current_state in ("half_open", "open"):
                state.delete(failures_key)
                state.set_kv(state_key, "closed")
                state.delete(open_until_key)
            return 0
        return 0

    async def _eval_str(script: str, numkeys: int, *args: object) -> str | None:
        keys = [str(a) for a in args[:numkeys]]
        argv = [str(a) for a in args[numkeys:]]
        # _CB_CHECK_STATE_LUA
        state_key = keys[0]
        open_until_key = keys[1]
        current_timestamp = float(argv[0]) if argv else time.time()
        current_state = state.get(state_key)
        if not current_state:
            return "closed"
        if current_state == "open":
            open_until = state.get(open_until_key)
            if open_until and current_timestamp >= float(open_until):
                state.set_kv(state_key, "half_open")
                return "half_open"
        return current_state

    return _eval_int, _eval_str


@pytest.fixture()
def fake_state():
    state = _FakeRedisState()
    eval_int, eval_str = _make_eval_helpers(state)
    with (
        patch("core.redis_client.redis_eval_int", side_effect=eval_int),
        patch("core.redis_client.redis_eval_str", side_effect=eval_str),
    ):
        yield state


@pytest.fixture()
def breaker() -> CircuitBreaker:
    return CircuitBreaker(
        name="test_cb",
        failure_threshold=3,
        recovery_timeout=0.3,
        failure_window=60,
        expected_exceptions=(ConnectionError,),
    )


async def _succeed() -> str:
    return "ok"


async def _fail() -> str:
    raise ConnectionError("boom")


class TestClosedState:
    async def test_successful_call_returns_value(self, breaker: CircuitBreaker, fake_state: _FakeRedisState) -> None:
        assert await breaker.call_async(_succeed) == "ok"

    async def test_remains_closed_after_success(self, breaker: CircuitBreaker, fake_state: _FakeRedisState) -> None:
        await breaker.call_async(_succeed)
        assert await breaker._check_state_async() == CircuitState.CLOSED


class TestOpeningCircuit:
    async def test_opens_after_threshold_failures(self, breaker: CircuitBreaker, fake_state: _FakeRedisState) -> None:
        for _ in range(breaker.failure_threshold):
            with pytest.raises(ConnectionError):
                await breaker.call_async(_fail)

        assert await breaker._check_state_async() == CircuitState.OPEN

    async def test_open_circuit_rejects_calls(self, breaker: CircuitBreaker, fake_state: _FakeRedisState) -> None:
        for _ in range(breaker.failure_threshold):
            with pytest.raises(ConnectionError):
                await breaker.call_async(_fail)

        with pytest.raises(CircuitBreakerOpenException):
            await breaker.call_async(_succeed)

    async def test_non_expected_exception_does_not_count(
        self, breaker: CircuitBreaker, fake_state: _FakeRedisState
    ) -> None:
        async def _value_error() -> str:
            raise ValueError("not counted")

        for _ in range(breaker.failure_threshold + 1):
            with pytest.raises(ValueError):
                await breaker.call_async(_value_error)

        assert await breaker._check_state_async() == CircuitState.CLOSED

    async def test_redis_state_check_failure_allows_call(
        self, breaker: CircuitBreaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        executed = False

        async def unavailable(*_: object) -> str:
            raise RuntimeError("redis unavailable")

        async def should_run() -> str:
            nonlocal executed
            executed = True
            return "ok"

        monkeypatch.setattr("core.redis_client.redis_eval_str", unavailable)

        result = await breaker.call_async(should_run)

        assert result == "ok"
        assert executed is True


class TestHalfOpenRecovery:
    async def test_transitions_to_half_open_after_timeout(
        self, breaker: CircuitBreaker, fake_state: _FakeRedisState
    ) -> None:
        for _ in range(breaker.failure_threshold):
            with pytest.raises(ConnectionError):
                await breaker.call_async(_fail)

        assert await breaker._check_state_async() == CircuitState.OPEN
        time.sleep(breaker.recovery_timeout + 0.05)
        assert await breaker._check_state_async() == CircuitState.HALF_OPEN

    async def test_success_in_half_open_closes_circuit(
        self, breaker: CircuitBreaker, fake_state: _FakeRedisState
    ) -> None:
        for _ in range(breaker.failure_threshold):
            with pytest.raises(ConnectionError):
                await breaker.call_async(_fail)

        time.sleep(breaker.recovery_timeout + 0.05)
        assert await breaker._check_state_async() == CircuitState.HALF_OPEN

        await breaker.call_async(_succeed)
        assert await breaker._check_state_async() == CircuitState.CLOSED

    async def test_failure_in_half_open_reopens(self, breaker: CircuitBreaker, fake_state: _FakeRedisState) -> None:
        for _ in range(breaker.failure_threshold):
            with pytest.raises(ConnectionError):
                await breaker.call_async(_fail)

        time.sleep(breaker.recovery_timeout + 0.05)

        with pytest.raises(ConnectionError):
            await breaker.call_async(_fail)

        assert await breaker._check_state_async() == CircuitState.OPEN


class TestDisabledBreaker:
    async def test_threshold_zero_always_passes_through(self, fake_state: _FakeRedisState) -> None:
        disabled = CircuitBreaker(name="disabled", failure_threshold=0)

        for _ in range(10):
            with pytest.raises(ConnectionError):
                await disabled.call_async(_fail)

        # Should still accept calls — never opens
        with pytest.raises(ConnectionError):
            await disabled.call_async(_fail)

    async def test_threshold_zero_success_works(self, fake_state: _FakeRedisState) -> None:
        disabled = CircuitBreaker(name="disabled", failure_threshold=0)
        assert await disabled.call_async(_succeed) == "ok"
