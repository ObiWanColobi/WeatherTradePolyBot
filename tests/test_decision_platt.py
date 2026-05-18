"""Tests for _calibrated_prob in weather_decision.

Platt is applied unconditionally — no flag check. Validated offline with V1
fit reproducing E1 + Phase 2 holdout dBrier +0.0155.
"""
import math

from calibration import apply_platt
from weather_decision import _calibrated_prob


def test_calibrated_prob_applies_platt():
    for raw in [0.01, 0.5, 0.99]:
        assert math.isclose(_calibrated_prob(raw), apply_platt(raw), abs_tol=1e-9)


def test_calibrated_prob_lifts_extreme_no_conviction():
    """Typical Phase 2 trade (raw P(YES)=0.012) lifts to ~0.087."""
    cal = _calibrated_prob(0.012)
    assert 0.07 < cal < 0.10


def test_calibrated_prob_stays_in_unit_interval():
    for raw in [0.0, 0.001, 0.5, 0.999, 1.0]:
        cal = _calibrated_prob(raw)
        assert 0.0 <= cal <= 1.0
