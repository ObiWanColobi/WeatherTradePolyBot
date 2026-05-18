"""Global Platt sigmoid calibration for raw GEFS-31 ensemble probabilities.

Convention (matches research_db/20_deliverable_E1_calibration.py):

    P_cal = sigmoid(a + b * logit(p_raw))

where a is the intercept and b is the slope. Defaults loaded from the V1 fit
at tasks/findings/_platt_refit_v1_validation_params.csv (smoothed row).
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

_PARAMS_CSV = Path(__file__).parent / "tasks" / "findings" / "_platt_refit_v1_validation_params.csv"

# Fallback if CSV missing — matches the V1 fit values from E1.
_FALLBACK_A = -1.11
_FALLBACK_B = 0.29


def _load_default_params() -> tuple[float, float]:
    if not _PARAMS_CSV.exists():
        return _FALLBACK_A, _FALLBACK_B
    with open(_PARAMS_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("fit_variant") == "smoothed":
                return float(row["a"]), float(row["b"])
    return _FALLBACK_A, _FALLBACK_B


PLATT_A, PLATT_B = _load_default_params()


def apply_platt(raw_prob: float, a: float | None = None, b: float | None = None) -> float:
    """Apply Platt sigmoid: P_cal = sigmoid(a + b * logit(raw_prob)).

    Defaults to the V1-fit (PLATT_A, PLATT_B) when a/b are not provided.
    Clips raw_prob into [eps, 1-eps] before taking logit to avoid inf.
    """
    if a is None:
        a = PLATT_A
    if b is None:
        b = PLATT_B
    eps = 1e-6
    p = max(eps, min(1.0 - eps, float(raw_prob)))
    logit = math.log(p / (1.0 - p))
    eta = a + b * logit
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, eta))))
