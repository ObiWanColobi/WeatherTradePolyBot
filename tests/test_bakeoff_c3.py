# tests/test_bakeoff_c3.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
import pytest
from bakeoff.candidates import c3_forecast_taker as c3
from bakeoff.harness.loader import CityDay

def _snap_row(lo, hi, tail, ask):
    return {"bound_lo_f": lo, "bound_hi_f": hi, "is_open_tail": tail,
            "best_ask": ask, "best_bid": ask - 0.02, "group_item_title": f"{lo}-{hi}"}

def test_c3_buys_when_calibrated_prob_exceeds_ask_by_threshold():
    # ensemble tightly around 71F so the 70-72 bucket is highly likely; ask is cheap (0.30)
    members = [71.0] * 30
    # EMOS params that keep sigma modest: a=0,b=1,c=1,d=1
    params = (0.0, 1.0, 1.0, 1.0)
    cd = CityDay(city="nyc", resolution_date="2026-06-01", kind="highest",
                 snapshots=None, truth_f=71.0)
    rows = [_snap_row(70.0, 72.0, 0, 0.30)]   # model ~ high, ask cheap -> edge -> BUY
    trades = c3.evaluate(cd, fee_rate=0.0, params=params,
                         member_temps=members, decision_snapshots=rows,
                         edge_threshold=0.05, stake_usd=50.0)
    assert len(trades) == 1
    assert trades[0]["side"] == "buy"
    assert trades[0]["shares"] > 0

def test_c3_no_buy_when_ask_exceeds_calibrated_prob():
    members = [71.0] * 30
    params = (0.0, 1.0, 1.0, 1.0)
    cd = CityDay(city="nyc", resolution_date="2026-06-01", kind="highest",
                 snapshots=None, truth_f=71.0)
    rows = [_snap_row(40.0, 42.0, 0, 0.90)]   # model ~ 0 for a far bucket, ask 0.90 -> no edge
    trades = c3.evaluate(cd, fee_rate=0.0, params=params,
                         member_temps=members, decision_snapshots=rows,
                         edge_threshold=0.05, stake_usd=50.0)
    assert trades == []
