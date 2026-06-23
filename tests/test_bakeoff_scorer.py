import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.scorer import settle_trade, score

def _buy(city, date, winset, shares, cost):
    return {"city": city, "resolution_date": date, "winset": winset,
            "side": "buy", "shares": shares, "net_cost_usd": cost, "fee_regime": "opt"}

def test_settle_winning_buy_pays_one_per_share():
    t = _buy("nyc", "2026-06-01", ("closed", [70, 71]), shares=10.0, cost=5.0)
    s = settle_trade(t, truth_f=70.4)  # rounds to 70, in [70,71] -> win
    assert s["payout_usd"] == pytest.approx(10.0)
    assert s["pnl_usd"] == pytest.approx(5.0)  # 10 payout - 5 cost

def test_settle_losing_buy_pays_zero():
    t = _buy("nyc", "2026-06-01", ("closed", [70, 71]), shares=10.0, cost=5.0)
    s = settle_trade(t, truth_f=80.0)
    assert s["payout_usd"] == pytest.approx(0.0)
    assert s["pnl_usd"] == pytest.approx(-5.0)

def test_score_city_majority_and_roi():
    trades = [
        _buy("nyc", "2026-06-01", ("closed", [70]), 10.0, 5.0),   # win +5
        _buy("dal", "2026-06-01", ("closed", [70]), 10.0, 5.0),   # lose -5
        _buy("dal", "2026-06-02", ("closed", [70]), 10.0, 5.0),   # lose -5
    ]
    truth = {("nyc","2026-06-01"):70.0, ("dal","2026-06-01"):80.0, ("dal","2026-06-02"):80.0}
    out = score(trades, truth)
    assert out["n_cities"] == 2
    # nyc +ROI, dal -ROI -> majority NOT positive (1 of 2)
    assert out["city_majority_positive"] is False
