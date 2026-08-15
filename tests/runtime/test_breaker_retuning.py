"""A breaker built once at import cannot be retuned; this is what makes it live."""

from core.circuit_breaker import CircuitBreaker
from core.resilience import LazyCircuitBreaker


def test_a_breaker_rebuilds_when_its_configuration_changes() -> None:
    settings = {"threshold": 3}
    built: list[int] = []

    def factory() -> CircuitBreaker:
        built.append(settings["threshold"])
        return CircuitBreaker(name="probe", failure_threshold=settings["threshold"], recovery_timeout=30.0)

    breaker = LazyCircuitBreaker(factory, signature=lambda: settings["threshold"])

    first = breaker._get()
    assert breaker._get() is first  # unchanged config reuses the instance
    assert built == [3]

    settings["threshold"] = 10
    rebuilt = breaker._get()

    assert rebuilt is not first
    assert rebuilt.failure_threshold == 10
    assert built == [3, 10]


def test_without_a_signature_it_still_builds_once() -> None:
    """The old behaviour, kept for breakers that have nothing to watch."""
    built: list[int] = []

    def factory() -> CircuitBreaker:
        built.append(1)
        return CircuitBreaker(name="probe", failure_threshold=3, recovery_timeout=30.0)

    breaker = LazyCircuitBreaker(factory)
    assert breaker._get() is breaker._get()
    assert built == [1]


def test_the_llm_breaker_follows_a_runtime_override(monkeypatch) -> None:
    """End to end: the value an operator types is the value the breaker uses."""
    from services.analysis import circuit_breakers
    from services.operations import runtime_settings as rt

    monkeypatch.setitem(rt._snapshot, "CIRCUIT_BREAKER_LLM_THRESHOLD", "2")
    first = circuit_breakers.llm_cb._get()
    assert first.failure_threshold == 2

    monkeypatch.setitem(rt._snapshot, "CIRCUIT_BREAKER_LLM_THRESHOLD", "9")
    assert circuit_breakers.llm_cb._get().failure_threshold == 9
