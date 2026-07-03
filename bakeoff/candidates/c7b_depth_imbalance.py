# bakeoff/candidates/c7b_depth_imbalance.py
"""Candidate #7b: order-book imbalance directional taker (NO weather forecast).

Edge hypothesis (mild, PREDICTIVE not pure-arb): near close, heavy one-sided resting
depth in a bucket's ladder reveals where informed/aggressive flow is leaning. A bucket
whose captured book is dominated by BIDS (people wanting to BUY that outcome) relative to
ASKS may be more likely to settle in-the-money than its ask price implies. We compute, per
bucket, at the fire-time snapshot:

    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)   in [-1, +1]

using ONLY the captured `orderbook_bids_json` / `orderbook_asks_json` levels (best-first).
When imbalance is strongly positive (>= IMBALANCE_THRESHOLD) AND the best_ask still leaves
room (ask <= MAX_ASK, i.e. we're not already paying near $1 for a "sure" bucket), we BUY
the bucket, walking the REAL captured ask ladder for the fill so we can never be paid on
shares that weren't actually resting. ROI is measured vs METAR truth by the scorer.

HONESTY:
  - Signal (imbalance) and fill both come ONLY from captured depth at the fire snapshot —
    no truth, no post-decision info (no lookahead).
  - Fills walk the real ask ladder via harness.depth.walk_book; shares filled never exceed
    captured ask size (plus a hard notional cap so one fat quote can't dominate).
  - This is the ONE candidate with a predictive claim, so it is held to the same calibration
    skepticism: a positive backtest is a forward-shadow candidate, NOT proof.

This candidate is depth-aware and is meant to be run through
bakeoff/run_microstructure_track.py, which hands it the fire-time decision_snapshots.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shotgun.bets import compute_winset  # noqa: E402
from bakeoff.harness.depth import parse_levels, walk_book  # noqa: E402
from bakeoff.harness.scorer import make_trade  # noqa: E402

NAME = "c7b_depth_imbalance"
FORECAST_BASED = False

# --- Signal / gating knobs (chosen a-priori, NOT tuned on truth — none is available yet) ---
IMBALANCE_THRESHOLD = 0.40   # require bids to out-weigh asks by >=40% net near close
MAX_ASK = 0.85               # don't chase buckets already priced as near-certain
MIN_ASK = 0.02               # skip dust books where a single share dominates ROI
STAKE_USD = 50.0             # intended notional per bucket (walked against real depth)
TOUCH_CAP_USD = 50.0         # hard notional cap: never assume more than this fills at a bucket
FIXED_COST_USD = 0.004       # gas, matches harness.cost_model


def _clean_json(val):
    """Return a parseable orderbook JSON string, or None if this cell has no captured depth.

    The parquet stores populated books as JSON strings but NULL cells come back as float
    NaN (pandas) — which is truthy, so we must NOT rely on `if val:`. Guard explicitly:
    only a non-empty, non-'null'/'[]' string is real captured depth.
    """
    if val is None:
        return None
    if isinstance(val, float):  # NaN sentinel for a missing cell
        return None
    if not isinstance(val, str):
        try:
            import pandas as pd
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        val = str(val)
    s = val.strip()
    if s in ("", "null", "None", "[]"):
        return None
    return s


def _num(val):
    """Coerce a best_bid/best_ask cell to float, or None if missing/NaN (no truthiness)."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _depth_sum(levels) -> float:
    """Total resting size across captured levels (best-first list of (price, size))."""
    return float(sum(float(s) for _p, s in levels))


def _depth_fill(ask_levels, shares_wanted: float, fee_rate: float) -> dict:
    """Walk the REAL captured ask ladder, cap notional, add fee+gas. cost_model-compatible.

    Returns a cost_result dict with the keys make_trade() / scorer need
    (filled_shares, net_cost_usd, ...). Fee uses the taker formula shares*rate*p*(1-p)
    at the volume-weighted avg fill price; gas is a flat per-trade cost.
    """
    walk = walk_book(ask_levels, shares_wanted)
    filled = float(walk["filled_shares"])
    avg = float(walk["avg_price"])
    if filled <= 0 or avg <= 0:
        return {"filled_shares": 0.0, "avg_price": 0.0, "fee_usd": 0.0,
                "gas_usd": 0.0, "unfilled_shares": float(shares_wanted), "net_cost_usd": 0.0}
    # Enforce the hard notional cap AFTER walking real depth: min(depth-limited, cap-limited).
    notional = filled * avg
    if notional > TOUCH_CAP_USD:
        filled = TOUCH_CAP_USD / avg
        notional = filled * avg
    fee = filled * fee_rate * avg * (1.0 - avg)
    gas = FIXED_COST_USD if filled > 0 else 0.0
    return {
        "filled_shares": filled,
        "avg_price": avg,
        "fee_usd": fee,
        "gas_usd": gas,
        "unfilled_shares": max(0.0, float(shares_wanted) - filled),
        "net_cost_usd": filled * avg + fee + gas,
    }


def evaluate(city_day, fee_rate, params, *, decision_snapshots,
             imbalance_threshold: float = IMBALANCE_THRESHOLD,
             max_ask: float = MAX_ASK, min_ask: float = MIN_ASK,
             stake_usd: float = STAKE_USD):
    """Emit BUY legs for buckets with strongly positive book imbalance at the fire snapshot.

    Follows the c3 contract: trades are built ONLY via harness.scorer.make_trade from a real
    depth-walked fill, so a leg can never be paid out on shares it could not fill.
    """
    trades = []
    if not decision_snapshots:
        return trades
    fee_regime = "opt" if fee_rate == 0.0 else "pess"

    for r in decision_snapshots:
        winset = compute_winset(r.get("bound_lo_f"), r.get("bound_hi_f"),
                                bool(r.get("is_open_tail")))
        if winset is None:
            continue

        # --- Signal: imbalance from CAPTURED depth only (no lookahead) ---
        # Require REAL captured JSON on BOTH sides — the imbalance ratio is only meaningful
        # when we actually observed both bid and ask depth. A synthetic fallback level would
        # fabricate a signal, so we skip any bucket without both books captured.
        bids_json = _clean_json(r.get("orderbook_bids_json"))
        asks_json = _clean_json(r.get("orderbook_asks_json"))
        if bids_json is None or asks_json is None:
            continue
        best_ask = _num(r.get("best_ask"))

        # Parse both sides. parse_levels sorts best-first; for the 'sell' side (bids) that is
        # descending price, for the 'buy' side (asks) ascending price.
        bid_levels = parse_levels(bids_json, None, "sell")
        ask_levels = parse_levels(asks_json, None, "buy")
        bid_depth = _depth_sum(bid_levels)
        ask_depth = _depth_sum(ask_levels)
        denom = bid_depth + ask_depth
        if denom <= 0:
            continue  # no captured two-sided depth -> no signal, no trade
        imbalance = (bid_depth - ask_depth) / denom
        if imbalance < imbalance_threshold:
            continue  # not strongly bid-heavy

        # --- Price gate: best_ask must leave room (not already ~certain, not dust) ---
        if best_ask is None or not (min_ask <= best_ask <= max_ask):
            continue

        # --- Fill: walk the REAL captured ask ladder; never invent liquidity ---
        shares_wanted = stake_usd / best_ask
        fill = _depth_fill(ask_levels, shares_wanted, fee_rate)
        if fill["filled_shares"] <= 0:
            continue

        trades.append(make_trade(city_day.city, city_day.resolution_date,
                                 winset, "buy", fee_regime, fill))
    return trades
