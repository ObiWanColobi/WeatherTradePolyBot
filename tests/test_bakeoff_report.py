import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from bakeoff.harness.report import render_verdict, scorecard_row

def test_render_verdict_has_all_rows_and_both_fee_columns():
    rows = [
        scorecard_row("c1_overround", "Stage 3",
                      opt_score={"roi": 0.05, "city_majority_positive": True, "bootstrap_ci_low": 0.01, "n_trades": 40},
                      pess_score={"roi": -0.02, "city_majority_positive": False, "bootstrap_ci_low": -0.1, "n_trades": 40},
                      calib=None),
    ]
    md = render_verdict(rows)
    assert "c1_overround" in md
    assert "low-fee" in md and "full-fee" in md  # both regimes shown
