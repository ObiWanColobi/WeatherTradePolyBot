"""Tests for calibration.apply_platt — global Platt sigmoid for GEFS-31 raw probs.

Convention (matches research_db/20_deliverable_E1_calibration.py and
research_db/41_platt_refit_v1_validation.py):

    P_cal = sigmoid(a + b * logit(p_raw))

where a is the intercept and b is the slope. Defaults loaded from the V1 fit
in tasks/findings/_platt_refit_v1_validation_params.csv (smoothed row).
"""
import math

import pytest

from calibration import PLATT_A, PLATT_B, apply_platt


def test_returns_probability_in_unit_interval():
    for raw in [0.001, 0.05, 0.25, 0.5, 0.85, 0.99]:
        cal = apply_platt(raw)
        assert 0.0 <= cal <= 1.0, f"raw={raw} produced {cal} outside [0,1]"


def test_identity_with_neutral_params():
    """a=0, b=1 must be the identity transform (sigmoid(logit(p)) = p)."""
    for raw in [0.05, 0.25, 0.5, 0.75, 0.95]:
        cal = apply_platt(raw, a=0.0, b=1.0)
        assert math.isclose(cal, raw, abs_tol=1e-6), f"raw={raw} got {cal}"


def test_v1_fit_compresses_high_extreme():
    """V1 fit (a=-1.11, b=0.29) compresses raw 0.99 strongly toward sigmoid(a)."""
    cal = apply_platt(0.99, a=-1.11, b=0.29)
    assert 0.5 < cal < 0.6, f"expected ~0.555, got {cal}"


def test_v1_fit_lifts_low_extreme():
    """V1 fit lifts raw 0.01 up toward sigmoid(a)."""
    cal = apply_platt(0.01, a=-1.11, b=0.29)
    assert 0.05 < cal < 0.12, f"expected ~0.080, got {cal}"


def test_v1_fit_at_logit_zero_returns_sigmoid_of_intercept():
    """For raw=0.5: logit=0, so P_cal = sigmoid(a) exactly."""
    cal = apply_platt(0.5, a=-1.11, b=0.29)
    expected = 1.0 / (1.0 + math.exp(1.11))
    assert math.isclose(cal, expected, abs_tol=1e-6)


def test_clips_extreme_inputs_without_crashing():
    """raw=0.0 and raw=1.0 must not produce inf/nan."""
    cal_zero = apply_platt(0.0)
    cal_one = apply_platt(1.0)
    assert 0.0 < cal_zero < 1.0
    assert 0.0 < cal_one < 1.0
    assert math.isfinite(cal_zero) and math.isfinite(cal_one)


def test_default_params_are_floats():
    assert isinstance(PLATT_A, float)
    assert isinstance(PLATT_B, float)


def test_default_params_match_v1_smoothed_fit():
    """Defaults must match the committed V1 fit (smoothed): a≈-1.1093, b≈+0.2944."""
    assert math.isclose(PLATT_A, -1.1093, abs_tol=1e-3), f"PLATT_A={PLATT_A}"
    assert math.isclose(PLATT_B,  0.2944, abs_tol=1e-3), f"PLATT_B={PLATT_B}"


def test_default_call_uses_loaded_params():
    """apply_platt(raw) without overrides must equal apply_platt(raw, PLATT_A, PLATT_B)."""
    for raw in [0.01, 0.1, 0.5, 0.9, 0.99]:
        assert apply_platt(raw) == apply_platt(raw, PLATT_A, PLATT_B)
