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
    # Truth scattered far wider than the (tight) ensemble spread. A correct EMOS fit
    # MUST recover an implied predictive sd approaching the true sd (~3.0), NOT stay at
    # the underdispersed starting point. We assert the implied sd is materially inflated
    # — this is the exact property whose absence killed the prior strategy. (A trivial
    # `c > 0.0` assertion is NOT sufficient: c starts at 1.0, so it would pass even if
    # the optimizer never inflated.)
    import random
    import numpy as np
    random.seed(1)
    records = []
    for _ in range(200):
        center = random.uniform(50, 80)
        members = [center + random.gauss(0, 0.5) for _ in range(20)]  # tight (underdispersed)
        truth = center + random.gauss(0, 3.0)  # actually wide
        records.append({"member_temps": members, "truth_f": truth})
    a, b, c, d = fit_emos(records)
    mean_svar = float(np.mean([np.var(r["member_temps"]) for r in records]))
    implied_sd = (c + d * mean_svar) ** 0.5
    # Truth sd is 3.0; a non-inflating fit gives ~1.1. Require real inflation.
    assert implied_sd > 2.0
