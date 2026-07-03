# bakeoff/candidates/c7_distributional_arb.py
"""Candidate #7: pure no-arbitrage on the bucket set (needs NO forecast).

Economic edge hypothesis
------------------------
For one city-day's highest-temperature market, the buckets are a *partition*: they
are mutually exclusive and — with the two open tails — exhaustive. Exactly one bucket
resolves YES (the observed integer-rounded daily max lands in exactly one win-set). So:

  * A full YES basket (one YES share of every bucket) pays exactly $1 at settlement,
    no matter what the weather does. If you can BUY the whole basket for
    sum(best_ask) < $1 - margin, you lock a riskless spread.
  * Symmetrically, selling one YES share of every bucket for sum(best_bid) > $1 + margin
    books a surplus: the basket's settlement obligation is exactly $1 (one leg you sold
    will resolve YES and cost you $1), so a > $1 credit is riskless profit.

This is the strongest edge hypothesis in the bake-off because it does NOT require
out-predicting the weather — it monetizes a *mispricing of the partition itself*.

Honesty rules enforced here
---------------------------
1. NO forecast, NO truth at decision time. The only decision input is the fire-time
   snapshot's captured order book. Truth is touched only at settlement (scorer).
2. Never fill more shares than captured depth supports. Each leg walks its REAL captured
   ladder (harness.depth.walk_book over parse_levels). The basket is fillable only up to
   the SHALLOWEST leg — you cannot hold a partial basket and still claim the riskless $1,
   so basket size = min over legs of each leg's depth-limited fillable shares. A leg whose
   book was not captured (only a zero-size synthetic touch) caps the basket at 0 → no trade.
3. Fees + gas modeled per leg via the same arithmetic as cost_model, at both regimes.
   The arbitrage must clear the *fees+gas margin*, not just $1.
4. Trades are built ONLY through scorer.make_trade from a cost_fill-shaped result, so the
   scorer can never pay out shares that were not actually fillable.

Settlement: each leg is an independent Trade on its bucket's win-set; the scorer settles
each vs METAR truth. Exactly one YES leg wins → a fully-filled YES basket nets +$1 gross
across its legs (minus cost); a SELL basket is the mirror.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shotgun.bets import compute_winset  # noqa: E402
from bakeoff.harness.depth import parse_levels, walk_book  # noqa: E402
from bakeoff.harness.cost_model import FIXED_COST_USD  # noqa: E402
from bakeoff.harness.scorer import make_trade  # noqa: E402

NAME = "c7_distributional_arb"
FORECAST_BASED = False

# Default margin (fraction of the $1 basket) required beyond fees+gas before we fire.
# A real dislocation must clear costs AND this cushion, not merely $1.00.
DEFAULT_MARGIN = 0.01

# Cap the basket notional in the same spirit as cost_model.TOUCH_CAP_USD: even with deep
# captured depth we don't assume we could shove unlimited size. Expressed in *basket units*
# (each basket unit = 1 share of every leg, costing ~$1), so this is ~ max $ per city-day.
MAX_BASKET_UNITS = 50.0


def _leg_ask_levels(row):
    """Captured ASK ladder for a leg (what a YES buyer consumes), best (lowest) first."""
    return parse_levels(row.get("orderbook_asks_json"), row.get("best_ask"), "buy")


def _leg_bid_levels(row):
    """Captured BID ladder for a leg (what a YES seller hits), best (highest) first."""
    return parse_levels(row.get("orderbook_bids_json"), row.get("best_bid"), "sell")


def _priceable_rows(decision_snapshots):
    """Rows with a valid win-set. Returns [(row, winset), ...]; empty if any leg is
    unpriceable (a malformed win-set means the partition is incomplete → no honest basket)."""
    out = []
    for r in decision_snapshots:
        ws = compute_winset(r.get("bound_lo_f"), r.get("bound_hi_f"), bool(r.get("is_open_tail")))
        if ws is None:
            return []  # partition broken → cannot claim exhaustive $1 basket
        out.append((r, ws))
    return out


def _fill_result(filled_shares, avg_price, fee_rate, side):
    """Build a cost_fill-shaped dict for ONE leg from an already depth-limited share count.

    Mirrors cost_model.cost_fill arithmetic (fee = shares*rate*p*(1-p), + gas once) but the
    fill quantity comes from a REAL depth walk, capped at the shallowest basket leg — never
    invented. For a SELL, net_cost_usd is negative (a credit received), so scorer P&L =
    payout(=obligation paid) - net_cost stays correct.
    """
    p = float(avg_price)
    fee = filled_shares * fee_rate * p * (1.0 - p)
    gas = FIXED_COST_USD if filled_shares > 0 else 0.0
    if side == "buy":
        net = filled_shares * p + fee + gas          # cash out
    else:  # sell: receive premium, still pay fee+gas
        net = -(filled_shares * p) + fee + gas        # credit (negative) net cost
    return {
        "filled_shares": filled_shares,
        "avg_price": p,
        "fee_usd": fee,
        "gas_usd": gas,
        "unfilled_shares": 0.0,
        "net_cost_usd": net,
    }


def evaluate(city_day, fee_rate, params, *, decision_snapshots,
             margin=DEFAULT_MARGIN, max_basket_units=MAX_BASKET_UNITS):
    """Emit the no-arbitrage basket legs for one city-day, or [] if no riskless spread.

    Signal (BUY basket): sum of leg best-asks < 1 - (est fees+gas margin) - margin.
    Signal (SELL basket): sum of leg best-bids > 1 + (est fees+gas margin) + margin.
    Fillable size = min over legs of that leg's REAL depth-walked shares at the target size
    (so we never claim a partial-basket $1). Legs with only a synthetic zero-size touch
    (uncaptured book) force basket size 0 → no trade.
    """
    fee_regime = "opt" if fee_rate == 0.0 else "pess"
    priced = _priceable_rows(decision_snapshots)
    if not priced:
        return []

    n_legs = len(priced)

    # --- Touch-level no-arb screen (uses only captured touch prices, no forecast) -------
    best_asks = [r.get("best_ask") for r, _ in priced]
    best_bids = [r.get("best_bid") for r, _ in priced]
    if any(a is None or not (0.0 < float(a) < 1.0) for a in best_asks):
        buy_cost = None
    else:
        buy_cost = sum(float(a) for a in best_asks)
    if any(b is None or not (0.0 < float(b) < 1.0) for b in best_bids):
        sell_credit = None
    else:
        sell_credit = sum(float(b) for b in best_bids)

    # Per-leg gas is charged once per leg; approximate the fee+gas margin at the touch.
    gas_margin = n_legs * FIXED_COST_USD  # in dollars, = fraction of the $1 basket

    do_buy = buy_cost is not None and buy_cost < (1.0 - gas_margin - margin)
    do_sell = sell_credit is not None and sell_credit > (1.0 + gas_margin + margin)

    # If both fire (crossed book), prefer the larger raw edge; never do both on one city-day.
    if do_buy and do_sell:
        buy_edge = (1.0 - gas_margin) - buy_cost
        sell_edge = sell_credit - (1.0 + gas_margin)
        if sell_edge > buy_edge:
            do_buy = False
        else:
            do_sell = False

    if not (do_buy or do_sell):
        return []

    side = "buy" if do_buy else "sell"

    # --- Depth-limit the basket to the SHALLOWEST leg (never invent liquidity) ----------
    # Walk each leg for the target size; the basket can only be as big as the leg that
    # runs out of captured depth first.
    target = max_basket_units
    leg_walks = []
    basket_units = target
    for r, ws in priced:
        levels = _leg_ask_levels(r) if side == "buy" else _leg_bid_levels(r)
        w = walk_book(levels, target)
        # A leg with no captured depth beyond a zero-size synthetic touch fills 0 shares.
        leg_walks.append((r, ws, levels))
        basket_units = min(basket_units, w["filled_shares"])

    if basket_units <= 0:
        return []  # some leg is unfillable → no honest riskless basket

    # --- Re-walk each leg to EXACTLY basket_units and settle avg price at real depth -----
    trades = []
    for r, ws, levels in leg_walks:
        w = walk_book(levels, basket_units)
        # Guard: floating error could leave a hair short; require the full basket size.
        if w["filled_shares"] + 1e-9 < basket_units:
            return []  # cannot honestly fill the whole basket → abandon (all-or-nothing)
        fill = _fill_result(basket_units, w["avg_price"], fee_rate, side)
        trades.append(make_trade(city_day.city, city_day.resolution_date,
                                 ws, side, fee_regime, fill))
    return trades
