# bakeoff/candidates/c3_forecast_taker.py
"""Candidate #3: EMOS calibrated-forecast taker. Buys buckets where calibrated P beats the ask."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shotgun.bets import compute_winset, winset_density
from shotgun.bucket_boundaries import build_ladder
from bakeoff.forecast.emos import bucket_probs
from bakeoff.harness.cost_model import cost_fill
from bakeoff.harness.scorer import make_trade

NAME = "c3_forecast_taker"
FORECAST_BASED = True


def evaluate(city_day, fee_rate, params, *, member_temps, decision_snapshots,
             edge_threshold=0.05, stake_usd=50.0):
    trades = []
    if not member_temps:
        return trades
    center = sum(member_temps) / len(member_temps)
    ladder = build_ladder(center)
    density = bucket_probs(member_temps, params, ladder)  # per-ladder-bucket calibrated P
    fee_regime = "opt" if fee_rate == 0.0 else "pess"
    for r in decision_snapshots:
        winset = compute_winset(r["bound_lo_f"], r["bound_hi_f"], bool(r["is_open_tail"]))
        if winset is None:
            continue
        ask = r.get("best_ask")
        if ask is None or not (0.0 < ask < 1.0):
            continue
        cal_p = winset_density(winset, ladder, density)
        if cal_p - ask <= edge_threshold:
            continue
        fill = cost_fill(ask, stake_usd / ask, fee_rate, "buy")
        if fill["filled_shares"] <= 0:
            continue
        trades.append(make_trade(city_day.city, city_day.resolution_date,
                                 winset, "buy", fee_regime, fill))
    return trades
