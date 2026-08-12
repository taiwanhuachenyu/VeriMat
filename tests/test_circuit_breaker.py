import pytest

from src.evaluation.circuit_breaker import CircuitOpenError, PersistentCircuitBreaker


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _breaker(tmp_path, clock, identifier="provider"):
    return PersistentCircuitBreaker(
        database=tmp_path / "circuits.db", circuit_id=identifier,
        failure_threshold=2, recovery_timeout_seconds=10,
        probe_timeout_seconds=20, clock=clock,
    )


def test_closed_open_half_open_success_cycle_is_persistent(tmp_path):
    clock = Clock()
    breaker = _breaker(tmp_path, clock)
    breaker.before_call(operation_id="one")
    breaker.record_failure(operation_id="one", reason_code="timeout")
    assert breaker.snapshot()["state"] == "CLOSED"
    breaker.before_call(operation_id="two")
    breaker.record_failure(operation_id="two", reason_code="timeout")
    assert breaker.snapshot()["state"] == "OPEN"
    with pytest.raises(CircuitOpenError, match="cooldown"):
        breaker.before_call(operation_id="blocked")

    clock.advance(10)
    breaker.before_call(operation_id="probe")
    assert breaker.snapshot()["state"] == "HALF_OPEN"
    with pytest.raises(CircuitOpenError, match="another probe"):
        breaker.before_call(operation_id="second-probe")
    breaker.record_success(operation_id="probe")
    assert breaker.snapshot()["state"] == "CLOSED"
    assert breaker.snapshot()["consecutive_failures"] == 0

    second_process = _breaker(tmp_path, clock)
    assert second_process.snapshot()["state"] == "CLOSED"
    assert [event["event_type"] for event in second_process.events()] == [
        "call_failed", "call_failed", "probe_admitted", "call_succeeded",
    ]


def test_failed_half_open_probe_reopens_and_requires_another_cooldown(tmp_path):
    clock = Clock()
    breaker = _breaker(tmp_path, clock)
    for operation in ("one", "two"):
        breaker.before_call(operation_id=operation)
        breaker.record_failure(operation_id=operation, reason_code="service_error")
    clock.advance(10)
    breaker.before_call(operation_id="probe")
    breaker.record_failure(operation_id="probe", reason_code="service_error")
    assert breaker.snapshot()["state"] == "OPEN"
    with pytest.raises(CircuitOpenError):
        breaker.before_call(operation_id="blocked")


def test_stale_half_open_probe_reopens_without_admitting_current_call(tmp_path):
    clock = Clock()
    breaker = _breaker(tmp_path, clock)
    for operation in ("one", "two"):
        breaker.before_call(operation_id=operation)
        breaker.record_failure(operation_id=operation, reason_code="timeout")
    clock.advance(10)
    breaker.before_call(operation_id="stale-probe")
    clock.advance(20)
    with pytest.raises(CircuitOpenError, match="expired"):
        breaker.before_call(operation_id="not-admitted")
    assert breaker.snapshot()["state"] == "OPEN"
    assert breaker.events()[-1]["event_type"] == "probe_expired"
