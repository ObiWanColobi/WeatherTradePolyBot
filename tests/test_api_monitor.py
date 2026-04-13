import time
from unittest.mock import patch, MagicMock

from api_monitor import CircuitBreaker, BreakerOpen, NonRetriableError


def test_breaker_starts_closed():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    assert cb.state == "CLOSED"


def test_breaker_trips_after_threshold_failures():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    assert cb.state == "CLOSED"
    cb.record_failure(retriable=True)
    assert cb.state == "OPEN"


def test_breaker_resets_failure_count_on_success():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb._fail_count == 0


def test_breaker_transitions_to_half_open_after_cooldown():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1])  # 100ms cooldown
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb.state == "OPEN"
    time.sleep(0.15)
    assert cb.state == "HALF_OPEN"


def test_breaker_half_open_success_closes():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1])
    for _ in range(3):
        cb.record_failure(retriable=True)
    time.sleep(0.15)
    assert cb.state == "HALF_OPEN"
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb._trip_count == 0


def test_breaker_half_open_failure_reopens_with_escalation():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1, 0.2])
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._trip_count == 1
    time.sleep(0.15)  # enter HALF_OPEN
    cb.record_failure(retriable=True)
    assert cb.state == "OPEN"
    assert cb._trip_count == 2


def test_breaker_escalating_cooldowns():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[10, 20, 30, 60])
    # First trip → 10s cooldown
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._current_cooldown == 10
    # Simulate half-open → failure → second trip → 20s cooldown
    cb._tripped_ts = time.time() - 11  # force past cooldown
    cb.record_failure(retriable=True)  # half-open → fail → reopen
    assert cb._current_cooldown == 20
    assert cb._trip_count == 2


def test_breaker_cooldown_caps_at_last_value():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[10, 20])
    # Trip 1 → 10s, Trip 2 → 20s, Trip 3+ → 20s (cap)
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._current_cooldown == 10
    cb._tripped_ts = time.time() - 11
    cb.record_failure(retriable=True)
    assert cb._current_cooldown == 20
    cb._tripped_ts = time.time() - 21
    cb.record_failure(retriable=True)
    assert cb._current_cooldown == 20  # stays at cap


def test_call_returns_result_when_closed():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    result = cb.call(lambda: 42)
    assert result == 42


def test_call_returns_none_when_open():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    for _ in range(3):
        cb.record_failure(retriable=True)
    result = cb.call(lambda: 42)
    assert result is None


def test_call_records_success_on_return():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    cb.call(lambda: "ok")
    assert cb._fail_count == 0


def test_call_records_failure_on_retriable_exception():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])

    def bad_call():
        raise ConnectionError("timeout")

    result = cb.call(bad_call)
    assert result is None
    assert cb._fail_count == 1


def test_non_retriable_error_raises_immediately():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])

    def auth_fail():
        raise NonRetriableError("401 Unauthorized")

    try:
        cb.call(auth_fail)
        assert False, "Should have raised"
    except NonRetriableError:
        pass
    # Failure count should NOT increment for non-retriable
    assert cb._fail_count == 0


@patch("api_monitor.notify")
def test_breaker_trip_sends_notification(mock_notify):
    cb = CircuitBreaker("test_notif", trip_threshold=3, cooldowns=[60])
    for _ in range(3):
        cb.record_failure(retriable=True)
    mock_notify.assert_called()
    call_args = mock_notify.call_args
    assert call_args[0][0] == "warning"
    assert "circuit breaker" in call_args[0][1].lower()


@patch("api_monitor.notify")
def test_breaker_recovery_sends_notification(mock_notify):
    cb = CircuitBreaker("test_recov", trip_threshold=3, cooldowns=[0.1])
    for _ in range(3):
        cb.record_failure(retriable=True)
    time.sleep(0.15)
    cb.record_success()
    # Should have been called twice: once for trip, once for recovery
    assert mock_notify.call_count >= 2
    last_call = mock_notify.call_args
    assert "Recovered" in last_call[0][1]


@patch("api_monitor.notify")
def test_non_retriable_sends_critical_notification(mock_notify):
    cb = CircuitBreaker("test_crit", trip_threshold=3, cooldowns=[60])

    def auth_fail():
        raise NonRetriableError("401 Unauthorized")

    try:
        cb.call(auth_fail)
    except NonRetriableError:
        pass
    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "critical"
