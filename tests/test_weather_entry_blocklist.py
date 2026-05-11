"""
Smoke tests for the entry_blocked_cities tampering-defense shim (2026-05-11).

Verifies:
- Tokyo is in the blocklist (from config)
- check_entry returns ok=False with the blocked-city reason for a Tokyo market
- A non-blocked city does NOT trip this gate

This is a minimal regression check. The full tier system will replace this
shim — see tasks/plans/2026-05-11_tampering_defense_tier_system.md.
"""
from config import WEATHER
from weather_entry import _BLOCKED_CITIES, check_entry


def _scan_data(city: str) -> dict:
    return {
        "city": city,
        "ensemble_n": 69,
        "yes_ensemble": 0,
        "ensemble_mean_c": 21.0,
        "threshold_c": 25.0,
        "hours_to_close": 24,
    }


def _market() -> dict:
    return {
        "price": 0.10,
        "volume24hr": 50000,
        "end_date": "2026-12-31T23:59:00Z",
    }


def test_tokyo_in_blocklist_config():
    """Config-side: tokyo is in entry_blocked_cities."""
    assert "tokyo" in WEATHER.get("entry_blocked_cities", [])


def test_blocklist_loaded_into_module():
    """Module-side: _BLOCKED_CITIES global is populated and lowercased."""
    assert "tokyo" in _BLOCKED_CITIES


def test_check_entry_blocks_tokyo():
    """check_entry returns ok=False with the blocked-city reason for tokyo."""
    decision = check_entry(_market(), _scan_data("tokyo"), direction="no")
    assert decision.ok is False
    assert "blocked" in decision.reason.lower() or "entry_blocked" in decision.reason.lower()
    assert "blocked_city" in decision.checks


def test_check_entry_blocks_tokyo_case_insensitive():
    """Even if scan_data passes 'Tokyo' or 'TOKYO', blocklist still fires."""
    for variant in ("Tokyo", "TOKYO", "tokyo"):
        decision = check_entry(_market(), _scan_data(variant), direction="no")
        assert decision.ok is False
        assert "blocked_city" in decision.checks, f"failed on variant: {variant!r}"


def test_check_entry_does_not_block_non_blocked_city():
    """A city not in the blocklist (e.g. 'hong kong') does not trip THIS gate.

    Other gates may still reject the entry; we only assert blocked_city is not
    the reason and is not in the checks dict.
    """
    decision = check_entry(_market(), _scan_data("hong kong"), direction="no")
    assert "blocked_city" not in decision.checks
