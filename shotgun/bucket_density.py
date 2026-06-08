"""Convert forecast (ensemble or deterministic) into 11-bucket density vector."""
from typing import Sequence

import numpy as np
from scipy.stats import norm

from shotgun.bucket_boundaries import daily_max_to_bucket_idx


def ensemble_to_density(
    members_f: Sequence[float],
    ladder: Sequence[tuple[float | None, float | None]],
) -> list[float]:
    """For an ensemble of member daily-max forecasts (in °F), count per-bucket
    membership and normalize."""
    n = len(members_f)
    if n == 0:
        return [0.0] * len(ladder)
    counts = [0] * len(ladder)
    for m in members_f:
        idx = daily_max_to_bucket_idx(m, ladder)
        counts[idx] += 1
    return [c / n for c in counts]


def deterministic_to_density(
    forecast_f: float,
    sigma_f: float,
    ladder: Sequence[tuple[float | None, float | None]],
) -> list[float]:
    """Convert deterministic forecast + per-city historical sigma into bucket density
    using normal CDF over each bucket's [lo, hi] range.
    Zero-sigma: all mass on containing bucket.
    """
    if sigma_f <= 0:
        idx = daily_max_to_bucket_idx(forecast_f, ladder)
        d = [0.0] * len(ladder)
        d[idx] = 1.0
        return d
    densities = []
    for lo, hi in ladder:
        lo_z = -np.inf if lo is None else (lo - forecast_f) / sigma_f
        hi_z = +np.inf if hi is None else (hi + 1 - forecast_f) / sigma_f  # +1 because integer ranges include hi value
        densities.append(float(norm.cdf(hi_z) - norm.cdf(lo_z)))
    s = sum(densities)
    if s <= 0:
        raise ValueError(
            f"deterministic_to_density: all bucket masses are zero "
            f"(forecast_f={forecast_f}, sigma_f={sigma_f})"
        )
    return [d / s for d in densities]
