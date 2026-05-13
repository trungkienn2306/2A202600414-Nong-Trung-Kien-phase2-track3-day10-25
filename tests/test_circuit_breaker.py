import time

import pytest

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def test_circuit_opens_after_failure_threshold_and_fails_fast() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=10)

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow_request()

    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "should not run")

    assert breaker.transition_log[-1]["to"] == "open"


def test_open_circuit_moves_to_half_open_after_timeout_then_closes_on_success() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=0.01)

    breaker.record_failure()
    time.sleep(0.02)

    assert breaker.allow_request()
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert [entry["to"] for entry in breaker.transition_log] == ["open", "half_open", "closed"]


def test_half_open_failure_reopens_immediately() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=0.01)

    breaker.record_failure()
    time.sleep(0.02)
    assert breaker.allow_request()

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.transition_log[-1]["reason"] == "probe_failed"
