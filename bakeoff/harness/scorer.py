"""Settle trades vs METAR truth; ROI, per-city robustness, bootstrap CI."""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shotgun.bets import winset_resolved  # pure import


def settle_trade(trade: dict, truth_f) -> dict:
    out = dict(trade)
    if truth_f is None:
        out["payout_usd"] = 0.0
        out["pnl_usd"] = 0.0
        out["unresolved"] = True
        return out
    won = winset_resolved(trade["winset"], float(truth_f))
    if trade["side"] == "sell":
        won = not won
    payout = trade["shares"] * 1.0 if won else 0.0
    out["payout_usd"] = payout
    out["pnl_usd"] = payout - trade["net_cost_usd"]
    out["unresolved"] = False
    return out


def score(trades: list[dict], truth_by_key: dict) -> dict:
    settled = []
    for t in trades:
        truth = truth_by_key.get((t["city"], t["resolution_date"]))
        s = settle_trade(t, truth)
        if not s.get("unresolved"):
            settled.append(s)
    if not settled:
        return {"roi": 0.0, "n_trades": 0, "n_cities": 0, "per_city_roi": {},
                "city_majority_positive": False, "bootstrap_ci_low": 0.0}
    total_cost = sum(s["net_cost_usd"] for s in settled)
    total_pnl = sum(s["pnl_usd"] for s in settled)
    roi = (total_pnl / total_cost) if total_cost > 0 else 0.0

    per_city: dict[str, list] = {}
    for s in settled:
        per_city.setdefault(s["city"], []).append(s)
    per_city_roi = {}
    for city, rows in per_city.items():
        c = sum(r["net_cost_usd"] for r in rows)
        p = sum(r["pnl_usd"] for r in rows)
        per_city_roi[city] = (p / c) if c > 0 else 0.0
    n_pos = sum(1 for v in per_city_roi.values() if v > 0)
    majority = n_pos > (len(per_city_roi) / 2.0)

    # Bootstrap CI on ROI (1000 resamples, 5th pct). Deterministic seed.
    rng = np.random.default_rng(12345)
    pnls = np.array([s["pnl_usd"] for s in settled])
    costs = np.array([s["net_cost_usd"] for s in settled])
    n = len(settled)
    rois = []
    for _ in range(1000):
        idx = rng.integers(0, n, n)
        c = costs[idx].sum()
        rois.append((pnls[idx].sum() / c) if c > 0 else 0.0)
    ci_low = float(np.percentile(rois, 5))

    return {
        "roi": roi, "n_trades": len(settled), "n_cities": len(per_city_roi),
        "per_city_roi": per_city_roi, "city_majority_positive": majority,
        "bootstrap_ci_low": ci_low,
    }
