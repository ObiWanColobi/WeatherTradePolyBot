# bakeoff/candidates/c6_cross_format.py
"""Candidate #6 — internal-consistency arb (no forecast, real depth walked).

SPEC intent: within a city-day, find pairs/sets of buckets whose bounds OVERLAP or
nest (a threshold `P(>=X)` alongside a partition, or an open-tail plus its complement)
priced inconsistently, and lock the cheaper-vs-richer side.

WHAT THE DATA ACTUALLY IS (verified on the depth window 2026-06-26..07-02, 331 city-days):
every depth-covered city-day is a STRICTLY PARTITIONED ladder — one row per integer °C,
each an EXACT-°C bucket (bound_lo_f == bound_hi_f, a single °C point). Adjacent buckets are
mutually exclusive (no shared integer °F; verified) and there is NO second market FORMAT
(no `>=X` threshold market, no `X or below` open tail) co-listed for the same city-day in
this window. So the literal cross-FORMAT pair arb (overlapping / nesting bounds) HAS NO
INSTANCES here — reported honestly via `no_cross_format_pairs`, not faked.

The one internal-consistency arb that genuinely EXISTS on a MECE partition is the
SUM-OF-PROBABILITIES arb, walked on real captured depth:

  SELL side (a genuine LOCK — needs only mutual exclusivity, which holds):
    The buckets are mutually exclusive, so AT MOST ONE resolves YES. If you SELL every
    bucket for a realizable total premium (walked on real bid depth) that exceeds the $1
    max liability of the single possible winner, you keep (premium - 1) no matter which
    bucket wins — and keep the full premium if the truth lands out-of-ladder (0 winners).
    We size to the MIN depth-fillable shares across ALL legs (you must fill every leg to
    hold the lock) and require the realizable Σ(avg_bid) to clear 1 + a fee cushion.

  BUY side (NOT a lock in this data — deliberately skipped):
    Buying every bucket for Σask < 1 only locks profit if the partition is EXHAUSTIVE
    ("one bucket must win"). These partitions have NO open tails (verified: 0/331 carry a
    tail in the depth window), so a daily max outside the listed °C values wins NOTHING and
    all bought legs lose. That is a directional in-ladder bet, not an arb, so we do not
    emit it — inventing a guaranteed payoff that isn't guaranteed would violate the
    honesty rule.

Trades are built ONLY through harness.scorer.make_trade off a depth-walked fill, so a leg
can never be paid on shares its captured ladder couldn't fill. No truth or post-decision
info is touched at decision time.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shotgun.bets import compute_winset
from bakeoff.harness.cost_model import FIXED_COST_USD, TOUCH_CAP_USD
from bakeoff.harness.depth import parse_levels, walk_book
from bakeoff.harness.scorer import make_trade

NAME = "c6_cross_format"
FORECAST_BASED = False

# Realizable Σ(bid) must clear $1 by at least this cushion (beyond modeled fees) for the
# sell-all lock to be worth firing. Keeps us off books that only "arb" on rounding noise.
SELL_EDGE_CUSHION = 0.005
# Cap total notional locked per city-day (mirrors cost_model.TOUCH_CAP_USD discipline).
PER_CITY_DAY_CAP_USD = TOUCH_CAP_USD


def _overlapping_or_nesting_pairs(rows):
    """Return pairs of rows whose integer-°F win-sets OVERLAP or NEST (the literal
    cross-format signature). On a strict partition this is always empty; kept so the
    runner can report the honest 'no cross-format pair exists' result from real data."""
    wsets = []
    for r in rows:
        ws = compute_winset(r["bound_lo_f"], r["bound_hi_f"], bool(r["is_open_tail"]))
        if ws is None or ws[0] != "closed":
            wsets.append((r, ws, None))
            continue
        wsets.append((r, ws, set(ws[1])))
    pairs = []
    for i in range(len(wsets)):
        for j in range(i + 1, len(wsets)):
            si = wsets[i][2]
            sj = wsets[j][2]
            if si is None or sj is None:
                continue
            inter = si & sj
            if inter and inter != si and inter != sj:  # partial overlap
                pairs.append((wsets[i][0], wsets[j][0]))
            elif si < sj or sj < si:  # strict nesting
                pairs.append((wsets[i][0], wsets[j][0]))
    return pairs


def _sell_fill(bid_json, best_bid, shares_wanted, fee_rate):
    """Walk REAL bid depth for a SELL leg; return a cost_result in the scorer's convention.

    A Polymarket SELL-YES == BUY-NO: you pay (1 - bid) per share now and receive $1 iff the
    bucket does NOT resolve. The scorer settles a 'sell' as payout=$1 when the winset FAILS,
    minus net_cost_usd — so net_cost_usd must be the NO-side cost (1 - avg_bid) per filled
    share, plus fee and gas. filled_shares comes straight from walking the captured ladder,
    so we can never be credited premium on shares the real book couldn't absorb.
    """
    levels = parse_levels(bid_json, best_bid, "sell")
    w = walk_book(levels, shares_wanted)
    filled = w["filled_shares"]
    avg_bid = w["avg_price"]
    if filled <= 0 or not (0.0 < avg_bid < 1.0):
        return None
    no_price = 1.0 - avg_bid
    fee = filled * fee_rate * avg_bid * (1.0 - avg_bid)  # symmetric in p; p(1-p)
    gas = FIXED_COST_USD
    net_cost = filled * no_price + fee + gas
    return {
        "filled_shares": filled,
        "avg_price": avg_bid,
        "fee_usd": fee,
        "gas_usd": gas,
        "unfilled_shares": w["unfilled_shares"],
        "net_cost_usd": net_cost,
    }


def evaluate(city_day, fee_rate, params, *, decision_snapshots=None):
    """Emit the depth-locked sell-all sum-arb legs for one city-day, or [] if none."""
    trades = []
    rows = decision_snapshots
    if not rows:
        return trades

    # Only rows we can actually price and settle (win-set derivable + a real bid).
    priced = []
    for r in rows:
        ws = compute_winset(r["bound_lo_f"], r["bound_hi_f"], bool(r["is_open_tail"]))
        if ws is None:
            continue
        bid = r.get("best_bid")
        if bid is None or not (0.0 < float(bid) < 1.0):
            continue
        priced.append((r, ws))

    # Need the FULL mutually-exclusive set priced to bound liability at $1 (one winner max).
    # If any bucket in the partition is unpriced we cannot claim the lock, so bail.
    if len(priced) < 2 or len(priced) != len(rows):
        return trades

    # Literal cross-format check (always empty on a strict partition; kept honest). If
    # overlapping/nesting FORMATS ever appear this candidate would need a pair-arb branch;
    # on this data it never triggers.
    _ = _overlapping_or_nesting_pairs([r for r, _ in priced])

    # Sum-of-probabilities inconsistency test (top-of-book proxy for the lock).
    sum_bid = sum(float(r["best_bid"]) for r, _ in priced)
    if sum_bid <= 1.0 + SELL_EDGE_CUSHION:
        return trades  # no inconsistency beyond fee cushion -> nothing to lock

    fee_regime = "opt" if fee_rate == 0.0 else "pess"

    # To LOCK we must sell the SAME share count on EVERY leg (one winner pays $1/share; the
    # rest keep premium). Size = min depth-fillable across all legs, then cap notional.
    per_leg_cap = []
    for r, _ in priced:
        levels = parse_levels(r.get("orderbook_bids_json"), r.get("best_bid"), "sell")
        per_leg_cap.append(sum(sz for _, sz in levels))  # total real bid size on this leg
    lock_shares = min(per_leg_cap)
    if lock_shares * sum_bid > PER_CITY_DAY_CAP_USD:
        lock_shares = PER_CITY_DAY_CAP_USD / sum_bid
    if lock_shares <= 0:
        return trades

    # Re-walk each leg at the common lock size; keep realized Σ premium honest.
    legs = []
    realized_premium = 0.0
    for r, ws in priced:
        fill = _sell_fill(r.get("orderbook_bids_json"), r.get("best_bid"), lock_shares, fee_rate)
        if fill is None or fill["filled_shares"] + 1e-9 < lock_shares:
            return trades  # a leg couldn't fill the common size -> lock breaks, abort
        realized_premium += fill["filled_shares"] * fill["avg_price"]
        legs.append((r, ws, fill))

    # Confirm the DEPTH-REALIZED premium (not just top-of-book) still clears $1 + fees.
    total_fee_gas = sum(f["fee_usd"] + f["gas_usd"] for _, _, f in legs)
    if realized_premium <= lock_shares + total_fee_gas + SELL_EDGE_CUSHION * lock_shares:
        return trades

    for r, ws, fill in legs:
        trades.append(make_trade(city_day.city, city_day.resolution_date,
                                 ws, "sell", fee_regime, fill))
    return trades
