import pytest

from app.resilience import CircuitBreaker, CircuitOpenError


def test_circuit_breaker_opens_after_repeated_failures():
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)

    for _ in range(2):
        with pytest.raises(ValueError):
            breaker.call(lambda: (_ for _ in ()).throw(ValueError("down")))

    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "unreachable")
