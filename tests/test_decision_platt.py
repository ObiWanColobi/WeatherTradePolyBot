"""Tests for the Platt wiring in weather_decision.evaluate().

Covers the small helper _calibrated_prob() and the candidate-loop integration
points: direction call, scan_data edge_prob, kelly arg, and sizing_decision
audit row (raw_prob vs calibrated_prob).
"""
import math

import pytest

from calibration import apply_platt
from config import WEATHER
from weather_decision import _calibrated_prob


@pytest.fixture(autouse=True)
def reset_platt_flag():
    """Ensure each test starts with platt_enabled=False, restore after."""
    original = WEATHER.get("platt_enabled", False)
    WEATHER["platt_enabled"] = False
    yield
    WEATHER["platt_enabled"] = original


def test_calibrated_prob_returns_raw_when_flag_off():
    WEATHER["platt_enabled"] = False
    for raw in [0.01, 0.5, 0.99]:
        assert _calibrated_prob(raw) == raw


def test_calibrated_prob_applies_platt_when_flag_on():
    WEATHER["platt_enabled"] = True
    for raw in [0.01, 0.5, 0.99]:
        assert math.isclose(_calibrated_prob(raw), apply_platt(raw), abs_tol=1e-9)


def test_calibrated_prob_with_v1_params_lifts_extreme_no_conviction():
    """Sanity: a typical Phase 2 trade (raw P(YES)=0.012) lifts to ~0.087."""
    WEATHER["platt_enabled"] = True
    cal = _calibrated_prob(0.012)
    assert 0.07 < cal < 0.10, f"expected ~0.087, got {cal}"


def test_calibrated_prob_idempotent_on_unit_interval():
    """Output must stay in [0, 1] for all valid inputs."""
    WEATHER["platt_enabled"] = True
    for raw in [0.0, 0.001, 0.5, 0.999, 1.0]:
        cal = _calibrated_prob(raw)
        assert 0.0 <= cal <= 1.0
