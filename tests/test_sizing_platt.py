"""Tests for the Platt half-size A/B gate in kelly_size_with_diagnostics.

The flag interaction:
- platt_enabled=False → half_size has no effect; size matches pre-Platt behavior
- platt_enabled=True + platt_half_size=True → final size is halved
- platt_enabled=True + platt_half_size=False → no halving (full ramp after A/B passes)
"""
import pytest

from config import WEATHER
from weather_sizing import kelly_size_with_diagnostics


@pytest.fixture(autouse=True)
def reset_platt_flags():
    """Restore platt flags after each test."""
    saved = {
        "platt_enabled": WEATHER.get("platt_enabled", False),
        "platt_half_size": WEATHER.get("platt_half_size", True),
    }
    yield
    for k, v in saved.items():
        WEATHER[k] = v


def _base_args():
    """A solid Phase-2-style NO trade: high conviction, big edge, plenty of bankroll."""
    return dict(
        balance=4000.0, model_prob=0.05, market_price=0.85, direction="no",
        ensemble_n=69, days_to_resolution=1, unanimous=False,
    )


def test_flag_off_keeps_size_unchanged_regardless_of_half_size():
    WEATHER["platt_enabled"] = False
    WEATHER["platt_half_size"] = True
    size_off, _ = kelly_size_with_diagnostics(**_base_args())
    WEATHER["platt_half_size"] = False
    size_off2, _ = kelly_size_with_diagnostics(**_base_args())
    assert size_off == size_off2
    assert size_off > 0  # sanity — base trade should produce a real stake


def test_flag_on_half_size_on_halves_the_size():
    WEATHER["platt_enabled"] = True
    WEATHER["platt_half_size"] = False
    full, _ = kelly_size_with_diagnostics(**_base_args())
    WEATHER["platt_half_size"] = True
    half, _ = kelly_size_with_diagnostics(**_base_args())
    assert full > 0
    assert half == pytest.approx(round(full / 2.0, 2), abs=0.02)


def test_flag_on_full_size_matches_flag_off():
    """platt_enabled=True with platt_half_size=False = same stake as flag off."""
    WEATHER["platt_enabled"] = False
    off, _ = kelly_size_with_diagnostics(**_base_args())
    WEATHER["platt_enabled"] = True
    WEATHER["platt_half_size"] = False
    full, _ = kelly_size_with_diagnostics(**_base_args())
    assert off == full


def test_half_size_below_min_bet_floor_returns_zero():
    """A near-floor trade should be zeroed when half-size pushes it under $5."""
    WEATHER["platt_enabled"] = True
    WEATHER["platt_half_size"] = True
    # Tiny edge → kelly produces a small but non-zero stake; halved -> below floor.
    size, diag = kelly_size_with_diagnostics(
        balance=100.0, model_prob=0.55, market_price=0.50, direction="yes",
        ensemble_n=69, days_to_resolution=3,
    )
    assert size == 0.0
    assert diag["binding_constraint"] == "min_bet_floor"


def test_half_size_diagnostic_flag_is_recorded():
    """Diag should expose whether half-size was applied, for A/B audit."""
    WEATHER["platt_enabled"] = True
    WEATHER["platt_half_size"] = True
    _, diag = kelly_size_with_diagnostics(**_base_args())
    assert diag.get("platt_half_size_applied") is True

    WEATHER["platt_half_size"] = False
    _, diag = kelly_size_with_diagnostics(**_base_args())
    assert diag.get("platt_half_size_applied") is False
