import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.forecast.emos import fit_emos, bucket_probs

def test_bucket_probs_sum_to_one():
    params = (0.0, 1.0, 1.0, 1.0)  # μ=x̄, σ²=1+s²
    ladder = [(None, 60.0), (60.0, 62.0), (62.0, 64.0), (64.0, None)]
    probs = bucket_probs([61.0, 62.0, 63.0], params, ladder)
    assert sum(probs) == pytest.approx(1.0, abs=1e-6)
    assert all(p >= 0 for p in probs)

def test_fit_emos_inflates_variance_for_underdispersed():
    # Truth scattered wider than ensemble spread -> d or c must be > 0
    import random
    random.seed(1)
    records = []
    for _ in range(200):
        center = random.uniform(50, 80)
        members = [center + random.gauss(0, 0.5) for _ in range(20)]  # tight (underdispersed)
        truth = center + random.gauss(0, 3.0)  # actually wide
        records.append({"member_temps": members, "truth_f": truth})
    a, b, c, d = fit_emos(records)
    # Variance model must add dispersion beyond the tiny ensemble spread
    assert c > 0.0 or d > 1.0
