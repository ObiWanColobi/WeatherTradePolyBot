from unittest.mock import patch, MagicMock
import api_monitor


def test_open_meteo_uses_shared_breaker():
    """Verify open_meteo module uses api_monitor for circuit breaking."""
    # Reset any existing breaker state
    api_monitor._breakers.clear()

    breaker = api_monitor.get_breaker("open_meteo")
    assert breaker.state == "CLOSED"


def test_open_meteo_inline_breaker_removed():
    """Verify the old inline _cb_* variables are gone."""
    import markets.open_meteo as om
    assert not hasattr(om, "_cb_fail_count"), "Old inline _cb_fail_count should be removed"
    assert not hasattr(om, "_cb_tripped_ts"), "Old inline _cb_tripped_ts should be removed"
    assert not hasattr(om, "_CB_THRESHOLD"), "Old inline _CB_THRESHOLD should be removed"
    assert not hasattr(om, "_CB_COOLDOWN"), "Old inline _CB_COOLDOWN should be removed"
