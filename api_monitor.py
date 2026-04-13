"""Shared circuit breaker for external API calls."""

import time
from config import WEATHER


class NonRetriableError(Exception):
    """Raised for errors that should not be retried (auth failures, bad signatures)."""
    pass


class BreakerOpen(Exception):
    """Raised when a call is attempted while the breaker is open."""
    pass


class CircuitBreaker:
    """Per-API circuit breaker with escalating cooldowns.

    States:
        CLOSED    — normal operation, tracking consecutive failures
        OPEN      — requests blocked, cooldown timer running
        HALF_OPEN — cooldown expired, next call is a test
    """

    def __init__(self, name: str, trip_threshold: int = 3, cooldowns: list[int] = None):
        self.name = name
        self._trip_threshold = trip_threshold
        self._cooldowns = cooldowns or WEATHER.get(
            "api_breaker_cooldowns", [120, 300, 600, 1800, 3600]
        )
        self._fail_count = 0
        self._trip_count = 0
        self._tripped_ts: float = 0
        self._current_cooldown: float = 0
        self._is_open = False

    @property
    def state(self) -> str:
        if not self._is_open:
            return "CLOSED"
        elapsed = time.time() - self._tripped_ts
        if elapsed >= self._current_cooldown:
            return "HALF_OPEN"
        return "OPEN"

    def record_success(self) -> None:
        """Record a successful API call. Resets breaker if HALF_OPEN."""
        self._fail_count = 0
        if self._is_open:
            # Recovery from HALF_OPEN
            self._is_open = False
            self._trip_count = 0
            print(f"[api_monitor] {self.name} circuit breaker recovered")

    def record_failure(self, retriable: bool = True) -> None:
        """Record a failed API call."""
        if not retriable:
            return  # non-retriable errors don't affect the breaker

        state = self.state
        if state == "HALF_OPEN":
            # Test call failed — re-open with escalated cooldown
            self._trip()
            return

        self._fail_count += 1
        if self._fail_count >= self._trip_threshold:
            self._trip()

    def _trip(self) -> None:
        """Trip the breaker (CLOSED→OPEN or HALF_OPEN→OPEN)."""
        self._is_open = True
        self._tripped_ts = time.time()
        self._trip_count += 1
        idx = min(self._trip_count - 1, len(self._cooldowns) - 1)
        self._current_cooldown = self._cooldowns[idx]
        self._fail_count = 0
        print(
            f"[api_monitor] {self.name} circuit breaker tripped "
            f"(trip #{self._trip_count}, cooldown {self._current_cooldown}s)"
        )

    def call(self, fn, *args, **kwargs):
        """Execute fn through the circuit breaker.

        Returns None if breaker is OPEN (call skipped).
        Raises NonRetriableError immediately if fn raises one.
        Returns None on retriable exceptions (and records failure).
        """
        state = self.state
        if state == "OPEN":
            return None

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except NonRetriableError:
            raise  # propagate immediately, don't affect breaker
        except Exception:
            self.record_failure(retriable=True)
            return None


# ── Global breaker instances ──────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """Get or create a circuit breaker by name."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name,
            trip_threshold=WEATHER.get("api_breaker_trip_threshold", 3),
            cooldowns=WEATHER.get("api_breaker_cooldowns", [120, 300, 600, 1800, 3600]),
        )
    return _breakers[name]


def call(name: str, fn, *args, **kwargs):
    """Convenience: get_breaker(name).call(fn, ...)."""
    return get_breaker(name).call(fn, *args, **kwargs)
