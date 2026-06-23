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

def test_make_trade_uses_filled_shares_not_wanted():
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).parent.parent))
    from bakeoff.harness.scorer import make_trade
    # cost result where only 100 of 200 wanted shares filled
    cost_result = {"filled_shares": 100.0, "net_cost_usd": 50.004, "unfilled_shares": 100.0,
                   "avg_price": 0.50, "fee_usd": 0.0, "gas_usd": 0.004}
    t = make_trade("nyc", "2026-06-01", ("closed", [70]), "buy", "opt", cost_result)
    assert t["shares"] == pytest.approx(100.0)        # filled, NOT 200 wanted
    assert t["net_cost_usd"] == pytest.approx(50.004)

def test_cluster_bootstrap_is_deterministic_and_runs():
    trades = [
        {"city":"nyc","resolution_date":"2026-06-01","winset":("closed",[70]),"side":"buy",
         "shares":10.0,"net_cost_usd":5.0,"fee_regime":"opt"},
        {"city":"nyc","resolution_date":"2026-06-01","winset":("closed",[71]),"side":"buy",
         "shares":10.0,"net_cost_usd":5.0,"fee_regime":"opt"},
        {"city":"dal","resolution_date":"2026-06-02","winset":("closed",[70]),"side":"buy",
         "shares":10.0,"net_cost_usd":5.0,"fee_regime":"opt"},
    ]
    truth = {("nyc","2026-06-01"):70.0, ("dal","2026-06-02"):80.0}
    out1 = score(trades, truth)
    out2 = score(trades, truth)
    assert out1["bootstrap_ci_low"] == out2["bootstrap_ci_low"]  # deterministic (seed)
