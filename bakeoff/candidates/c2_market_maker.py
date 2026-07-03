# bakeoff/candidates/c2_market_maker.py
"""Candidate #2: passive two-sided market-making on wide-spread liquid buckets.

Economic hypothesis (one line): on a wide-spread but liquid weather bucket you can post
passive quotes just inside the touch, earn the captured spread + maker rebate, and net a
profit IF that spread exceeds the adverse-selection cost of getting filled only when the
market moves against you (plus gas).

No forecast is used. This is a MICROSTRUCTURE candidate: it reads the REAL captured
orderbook ladders (`orderbook_bids_json` / `orderbook_asks_json`) surfaced by the
snapshot loader + decision.decision_rows, and walks them with harness.depth.

── Honesty model ─────────────────────────────────────────────────────────────────
DECISION (lookahead-safe): at the fire-time snapshot only, for each priceable bucket with
a spread >= MIN_SPREAD and a liquid touch, we decide to post a passive bid at
best_bid + TICK and a passive ask at best_ask - TICK (i.e. "just inside the touch"). We
size each quote to the captured touch liquidity (never more than the ladder shows).

FILL (market-evolution, NOT truth): a passive quote fills only if the market later CROSSES
it. We approximate that by scanning the SAME city-day's snapshots that occur strictly AFTER
the fire snapshot and checking whether best_bid/best_ask ever reached our quoted price.
This uses no truth and no post-close information — only the order book evolving in time.
Adverse selection is baked in structurally: a passive BID fills exactly when price fell to
it (we bought a bucket the market is marking down); a passive ASK (a sell / short of the
bucket) fills exactly when price rose to it. Settlement vs METAR truth then reveals whether
that fill was a winner or a loser. We do NOT preferentially skip the losing side — every
quote the market crossed becomes a trade, so the P&L carries the full adverse-selection tax.

SETTLEMENT: every trade is built via harness.scorer.make_trade from a maker cost_fill, so
shares == filled_shares (capped by captured touch depth) and net_cost_usd come from the
SAME fill — the candidate can never be paid out on shares it could not fill. Maker fills pay
NO taker fee (maker = free / rebate per reference_polymarket_fee_2026_weather); the dual
fee_rate still flows into cost_fill so a taker-fee regime would be honored if ever applied,
and gas is always charged.

CAVEAT (why realized MM edge is hard to prove from snapshots): our fill proxy assumes we
are first in the queue at the touch and that any later touch-cross fully fills our resting
size. Real queue priority, partial fills, and cancel/replace races would all REDUCE realized
edge. So a positive result here is an OPTIMISTIC upper bound and a forward-shadow candidate,
never proof.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shotgun.bets import compute_winset
from bakeoff.harness.depth import parse_levels
from bakeoff.harness.scorer import make_trade

NAME = "c2_market_maker"
FORECAST_BASED = False

# --- MM parameters (all decided at fire time; none use truth) ----------------------
TICK = 0.001          # step just inside the touch when posting a passive quote
MIN_SPREAD = 0.03     # only quote buckets whose captured bid-ask spread is >= 3c
MIN_TOUCH_SIZE = 5.0  # require at least this many shares of captured depth at the touch
MAX_QUOTE_SHARES = 200.0  # cap resting size per quote (don't assume we soak the whole book)
FIXED_COST_USD = 0.004    # gas per fill (matches cost_model.FIXED_COST_USD)


def _maker_fill(price: float, shares: float, fee_rate: float, side: str) -> dict:
    """Maker fill at our passive quote price. Maker = free/rebate, so no taker fee is
    charged even when a taker fee_rate is supplied; gas is always paid. Returns the same
    dict shape as cost_model.cost_fill so make_trade can consume it unchanged.

    fee_rate is threaded through only so a future maker-fee/rebate regime could be modeled;
    at both current regimes (0.0, 0.05) the maker fee is 0.0 by policy.
    """
    p = float(price)
    sh = float(shares)
    if p <= 0.0 or p >= 1.0 or sh <= 0.0:
        return {"filled_shares": 0.0, "avg_price": 0.0, "fee_usd": 0.0,
                "gas_usd": 0.0, "unfilled_shares": sh, "net_cost_usd": 0.0}
    maker_fee = 0.0  # maker rebate/free per reference_polymarket_fee_2026_weather
    gas = FIXED_COST_USD
    net = sh * p + maker_fee + gas
    return {"filled_shares": sh, "avg_price": p, "fee_usd": maker_fee,
            "gas_usd": gas, "unfilled_shares": 0.0, "net_cost_usd": net}


def _touch_size(ob_json, side: str) -> float:
    """Captured shares resting at the best level of the given side (0 if no depth)."""
    levels = parse_levels(ob_json, None, side)
    if not levels:
        return 0.0
    # parse_levels sorts best-first; the first level is the touch.
    return float(levels[0][1])


def _later_extremes(snapshots: pd.DataFrame, fire_ts, sub_id) -> tuple:
    """Over the SAME bucket's snapshots strictly AFTER fire_ts, return
    (min_best_bid, max_best_ask) — the deepest the market dipped and the highest it lifted.

    These bracket whether a passive quote posted at fire time was ever crossed. Uses only
    same-day order-book evolution; no truth, no post-close data.
    """
    if snapshots is None or len(snapshots) == 0 or fire_ts is None:
        return None, None
    df = snapshots[snapshots["sub_market_condition_id"] == sub_id]
    if df.empty:
        return None, None
    ts = pd.to_datetime(df["snapshot_at_utc"], errors="coerce")
    fire = pd.to_datetime(fire_ts, errors="coerce")
    after = df[ts > fire]
    if after.empty:
        return None, None
    bids = pd.to_numeric(after["best_bid"], errors="coerce").dropna()
    asks = pd.to_numeric(after["best_ask"], errors="coerce").dropna()
    min_bid = float(bids.min()) if not bids.empty else None
    max_ask = float(asks.max()) if not asks.empty else None
    return min_bid, max_ask


def evaluate(city_day, fee_rate, params, *, decision_snapshots=None):
    """Post passive two-sided quotes on wide-spread liquid buckets; fill only where the
    market later crosses the quote. Returns a list of settled-shape trades (via make_trade).
    """
    trades = []
    from bakeoff.harness.decision import decision_rows
    rows = decision_snapshots
    fire_ts = None
    if city_day.snapshots is not None and len(city_day.snapshots) > 0:
        fire_ts, derived = decision_rows(city_day.snapshots)
        if rows is None:
            rows = derived
    if not rows:
        return trades

    fee_regime = "opt" if fee_rate == 0.0 else "pess"

    for r in rows:
        winset = compute_winset(r["bound_lo_f"], r["bound_hi_f"], bool(r["is_open_tail"]))
        if winset is None:
            continue
        bid = r.get("best_bid")
        ask = r.get("best_ask")
        if bid is None or ask is None:
            continue
        try:
            bid = float(bid)
            ask = float(ask)
        except (TypeError, ValueError):
            continue
        if not (0.0 < bid < ask < 1.0):
            continue
        spread = ask - bid
        if spread < MIN_SPREAD:
            continue  # not worth quoting inside a tight book

        sub_id = r.get("sub_market_condition_id")
        bid_touch = _touch_size(r.get("orderbook_bids_json"), "sell")   # depth resting on bid side
        ask_touch = _touch_size(r.get("orderbook_asks_json"), "buy")    # depth resting on ask side

        # Prices of our passive quotes: just INSIDE the touch.
        my_bid_px = round(bid + TICK, 4)
        my_ask_px = round(ask - TICK, 4)
        if not (0.0 < my_bid_px < my_ask_px < 1.0):
            continue

        min_bid, max_ask = _later_extremes(city_day.snapshots, fire_ts, sub_id)

        # --- BID side: we posted to BUY the bucket at my_bid_px. It fills if the market
        # later traded DOWN to our price (best_bid dipped to <= my_bid_px). Size = min of
        # captured touch depth and our cap. This is a "buy" (long the winset). ---
        if bid_touch >= MIN_TOUCH_SIZE and min_bid is not None and min_bid <= my_bid_px:
            shares = min(bid_touch, MAX_QUOTE_SHARES)
            fill = _maker_fill(my_bid_px, shares, fee_rate, "buy")
            if fill["filled_shares"] > 0:
                trades.append(make_trade(city_day.city, city_day.resolution_date,
                                         winset, "buy", fee_regime, fill))

        # --- ASK side: we posted to SELL the bucket at my_ask_px. It fills if the market
        # later traded UP to our price (best_ask lifted to >= my_ask_px). A "sell" pays out
        # when the winset does NOT occur (scorer inverts). ---
        if ask_touch >= MIN_TOUCH_SIZE and max_ask is not None and max_ask >= my_ask_px:
            shares = min(ask_touch, MAX_QUOTE_SHARES)
            fill = _maker_fill(my_ask_px, shares, fee_rate, "sell")
            if fill["filled_shares"] > 0:
                trades.append(make_trade(city_day.city, city_day.resolution_date,
                                         winset, "sell", fee_regime, fill))

    return trades
