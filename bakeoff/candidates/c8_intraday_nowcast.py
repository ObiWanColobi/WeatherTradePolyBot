# bakeoff/candidates/c8_intraday_nowcast.py
"""Candidate #8: intraday conditional-max nowcast (the last untested structural idea).

Economic edge hypothesis
------------------------
Midday (≈late morning to early afternoon local), the day's warming is partly OBSERVED but
the peak has not yet locked. Define:

    running_max_so_far(t) = max METAR temp observed on the city-local day up to fire time t
    remaining_warming      = final_daily_max - running_max_so_far(t)   (>= 0 by construction)

If the *market* is anchored on a stale morning model run while the *actual observed morning
trajectory* has already diverged (hotter or cooler than the model expected), then a
distribution over `final_daily_max = running_max_so_far + remaining_warming`, with the
remaining-warming term learned from history conditioned on local hour, can price the buckets
better than the market and reveal buys/sells the market hasn't caught up to.

This is the ONE forecast-free-ish idea none of the earlier bake-off candidates tested: it uses
REAL-TIME observed temperature (not a forecast, not just the order book). Prior is LOW — the
intraday obs game is crowded by second-scale bots and SaaS feeds — but our own data can
adjudicate it cheaply and honestly.

Honesty rules enforced here
---------------------------
1. NO lookahead in the signal. `running_max_so_far` uses ONLY weather_obs rows with
   observed_at <= fire_ts, same source/obs_type as SETTLEMENT truth (historical_archive /
   metar_hourly) so that at end-of-day the running max converges to the settled daily max —
   no phantom edge from a richer data source than the one we grade against.
2. The remaining-warming distribution is learned on the TUNE split ONLY (city-days strictly
   excluded from the scored/holdout set), bucketed by local hour-of-day. This city-day's own
   future is never in its own training data.
3. Trades price through cost_model.cost_fill and are built via scorer.make_trade, walking the
   REAL captured order book — never inventing shares, never booking both sides of a bucket
   (the "always get the good side" cheat that killed shotgun and candidate #2).
4. One side per bucket: we take the single best mispriced bucket-side per city-day family
   after fees, sized to captured depth, capped like the other candidates.

The model object (built once on tune data) is passed in via params["model"].
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shotgun.bets import compute_winset, winset_resolved  # noqa: E402
from bakeoff.harness.depth import parse_levels, walk_book  # noqa: E402
from bakeoff.harness.cost_model import FIXED_COST_USD  # noqa: E402
from bakeoff.harness.scorer import make_trade  # noqa: E402

NAME = "c8_intraday_nowcast"
FORECAST_BASED = False  # uses observed temps, not model forecasts

# Minimum model-vs-market edge (probability points) required before firing, after fees.
DEFAULT_EDGE = 0.08
# Depth/notional cap in shares per bucket-side (mirrors the spirit of cost_model.TOUCH_CAP_USD).
MAX_SHARES = 50.0


def _bucket_win_prob(winset, running_max_f, remaining_dist):
    """P(final integer-rounded daily max satisfies `winset` | running max, remaining-warming dist).

    remaining_dist: list of (delta_f, weight) samples of remaining_warming (final - running),
    all >= 0, weights summing to ~1. We convolve: final = running_max_f + delta, round, test
    membership. This is a plug-in Monte-Carlo over the empirical remaining-warming distribution.
    """
    p = 0.0
    for delta, w in remaining_dist:
        final_f = running_max_f + delta
        if winset_resolved(winset, final_f):
            p += w
    return p


def _fill_result(shares, avg_price, fee_rate, side):
    """cost_fill-shaped dict for one bucket-side; mirrors cost_model arithmetic exactly."""
    p = float(avg_price)
    fee = shares * fee_rate * p * (1.0 - p)
    gas = FIXED_COST_USD if shares > 0 else 0.0
    if side == "buy":
        net = shares * p + fee + gas
    else:  # sell YES: receive premium, pay fee+gas
        net = -(shares * p) + fee + gas
    return {"filled_shares": shares, "avg_price": p, "fee_usd": fee, "gas_usd": gas,
            "unfilled_shares": 0.0, "net_cost_usd": net}


def evaluate(city_day, fee_rate, params, *, decision_snapshots, running_max_f=None,
             local_hour=None, edge=DEFAULT_EDGE, max_shares=MAX_SHARES):
    """Emit at most ONE bucket-side trade for this city-day, or [] if no edge clears fees.

    running_max_f / local_hour are injected by the runner (lookahead-safe reconstruction from
    weather_obs at fire time). params["model"] is the tune-learned RemainingWarmingModel.
    """
    model = (params or {}).get("model")
    if model is None or running_max_f is None or local_hour is None:
        return []

    remaining_dist = model.dist_for_hour(local_hour)
    if not remaining_dist:
        return []  # no tune data for this local hour -> abstain

    fee_regime = "opt" if fee_rate == 0.0 else "pess"

    # Evaluate every bucket; keep the single best post-fee edge (one side, one bucket).
    best = None  # (net_edge, side, winset, row, model_p)
    for r in decision_snapshots:
        ws = compute_winset(r.get("bound_lo_f"), r.get("bound_hi_f"), bool(r.get("is_open_tail")))
        if ws is None:
            continue
        model_p = _bucket_win_prob(ws, running_max_f, remaining_dist)

        ask = r.get("best_ask")
        bid = r.get("best_bid")
        # BUY YES if model thinks it's underpriced: model_p - ask > edge.
        if ask is not None and 0.0 < float(ask) < 1.0:
            raw = model_p - float(ask)
            if raw > edge and (best is None or raw > best[0]):
                best = (raw, "buy", ws, r, model_p)
        # SELL YES if model thinks it's overpriced: bid - model_p > edge.
        if bid is not None and 0.0 < float(bid) < 1.0:
            raw = float(bid) - model_p
            if raw > edge and (best is None or raw > best[0]):
                best = (raw, "sell", ws, r, model_p)

    if best is None:
        return []

    _, side, ws, row, _model_p = best

    # Walk the REAL captured book for this side; never invent liquidity.
    if side == "buy":
        levels = parse_levels(row.get("orderbook_asks_json"), row.get("best_ask"), "buy")
    else:
        levels = parse_levels(row.get("orderbook_bids_json"), row.get("best_bid"), "sell")
    w = walk_book(levels, max_shares)
    shares = w["filled_shares"]
    if shares <= 0:
        return []  # no captured depth on the side we want -> no honest trade

    fill = _fill_result(shares, w["avg_price"], fee_rate, side)
    return [make_trade(city_day.city, city_day.resolution_date, ws, side, fee_regime, fill)]
