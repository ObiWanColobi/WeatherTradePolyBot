import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.calibration_gate import calibration_gate

def test_gate_fails_when_emos_worse_than_raw():
    import random
    random.seed(2)
    records = []
    for _ in range(100):
        c = random.uniform(50, 80)
        records.append({"member_temps": [c]*20, "truth_f": c + random.gauss(0, 2)})
    params = (0.0, 1.0, 4.0, 1.0)
    # raw_crps absurdly good (0.0) so EMOS cannot beat it -> gate fails
    out = calibration_gate(records, params, raw_crps=0.0)
    assert out["beats_raw"] is False
    assert out["passed"] is False
