"""Settle shotgun fires whose city-day daily-max is known. Holds-to-resolution:
win -> shares*$1, loss -> $0. No early exit. Credits balance, marks closed."""
from __future__ import annotations

import json
import db
from shotgun.bets import winset_resolved


def settle_fire(fire_id: int, daily_max_f: float) -> int:
    """Settle every open leg of one fire against the known daily-max.

    YES leg wins iff the bucket is hit; NO leg wins iff the bucket is NOT hit.
    Winners credit balance by shares*$1 (the $1-per-share resolution payout);
    losers credit nothing (their stake was already debited at fire time). Every
    leg is marked closed with resolved_outcome + pnl, and the fire is closed.

    Returns the number of legs settled.
    """
    bets = db.get_all_bets_for_fire(fire_id)
    n = 0
    for b in bets:
        if b["status"] != "open":
            continue
        winset = (b["winset_kind"], json.loads(b["winset_payload_json"]))
        bucket_hit = winset_resolved(winset, daily_max_f)
        won = bucket_hit if b["side"] == "yes" else (not bucket_hit)
        proceeds = b["shares"] * 1.0 if won else 0.0
        pnl = proceeds - b["stake_usd"]
        db.update_bet(b["id"], dict(
            status="closed", resolved_outcome=("win" if won else "loss"), pnl=pnl))
        if proceeds > 0:
            db.update_balance(proceeds)
        n += 1
    db.update_fire(fire_id, dict(status="closed"))
    db.record_account_value()
    return n


def settle_due_fires(truth_fn) -> int:
    """For each open fire whose city-day daily-max is known (truth_fn returns a
    value), settle it. truth_fn(city, resolution_date) -> daily_max_f | None."""
    total = 0
    for fire in db.get_open_fires():
        dm = truth_fn(fire["city"], fire["resolution_date"])
        if dm is None:
            continue
        total += settle_fire(fire["id"], float(dm))
    return total


def settle_leg_polymarket(bet: dict, resolution_fetch) -> bool:
    """Settle ONE open leg using Polymarket's resolution. Returns True if it was
    settled (resolved), False if not resolved yet / fetch failed (leg left open).
    resolution_fetch(condition_id) -> get_resolution_status shape (injected for tests)."""
    if bet["status"] != "open":
        return False
    res = resolution_fetch(bet["sub_market_condition_id"])
    if not res or not res.get("resolved"):
        return False
    bucket_hit = res.get("yes_price") == 1.0
    won = bucket_hit if bet["side"] == "yes" else (not bucket_hit)
    proceeds = bet["shares"] * 1.0 if won else 0.0
    pnl = proceeds - bet["stake_usd"]
    db.update_bet(bet["id"], dict(
        status="closed", resolved_outcome=("win" if won else "loss"), pnl=pnl))
    if proceeds > 0:
        db.update_balance(proceeds)
    return True


def settle_shadow_leg_polymarket(shadow: dict, resolution_fetch) -> bool:
    """Settle ONE open shadow (counterfactual) leg via Polymarket resolution.
    Records what would_side WOULD have earned on a $1 notional stake — credits
    NO balance (it was never placed). Returns True if it resolved."""
    if shadow["status"] != "open":
        return False
    res = resolution_fetch(shadow["sub_market_condition_id"])
    if not res or not res.get("resolved"):
        return False
    bucket_hit = res.get("yes_price") == 1.0
    won = bucket_hit if shadow["would_side"] == "yes" else (not bucket_hit)
    # $1 notional: win pays (1/mid)*1 - 1 = (1-mid)/mid on YES; symmetric for NO
    # via the NO cost (1-mid). Simpler/robust: P&L = payout - cost on $1 stake.
    cost = float(shadow["mid_price"]) if shadow["would_side"] == "yes" else (1.0 - float(shadow["mid_price"]))
    if cost <= 0.0:
        hypo_pnl = 0.0   # degenerate price — no meaningful counterfactual
    else:
        shares = 1.0 / cost           # $1 buys this many shares of the would-side token
        hypo_pnl = (shares * 1.0 - 1.0) if won else (-1.0)
    db.update_shadow_bet(shadow["id"], dict(
        status="closed", resolved_outcome=("win" if won else "loss"), hypo_pnl=hypo_pnl))
    return True


def settle_due_fires_polymarket(resolution_fetch=None) -> int:
    """For every open fire, try to settle each open leg via Polymarket resolution.
    A fire is closed only once all its REAL legs are closed. Also settles open
    shadow (counterfactual) legs through the same resolution — they record
    hypo_pnl but credit no balance, and do NOT gate fire closure.
    Returns # real legs settled. resolution_fetch defaults to get_resolution_status."""
    if resolution_fetch is None:
        from markets.polymarket import get_resolution_status as resolution_fetch
    settled = 0
    for fire in db.get_open_fires():
        legs = db.get_all_bets_for_fire(fire["id"])
        for leg in legs:
            if leg["status"] != "open":
                continue
            if settle_leg_polymarket(leg, resolution_fetch):
                settled += 1
        # Settle shadow legs too (no balance effect, no closure gating).
        for shadow in db.get_shadow_bets_for_fire(fire["id"]):
            if shadow["status"] == "open":
                try:
                    settle_shadow_leg_polymarket(shadow, resolution_fetch)
                except Exception as e:
                    print(f"[resolve] shadow settle failed id={shadow.get('id')}: {e}")
        # re-read legs; close the fire only if no REAL leg remains open
        still_open = [b for b in db.get_all_bets_for_fire(fire["id"]) if b["status"] == "open"]
        if not still_open:
            db.update_fire(fire["id"], dict(status="closed"))
    if settled:
        db.record_account_value()
    return settled


def live_truth(city: str, resolution_date: str) -> float | None:
    """Observed daily-max °F for (city, resolution_date) from the live DB, or None
    if not yet known.

    NOTE: this temperature-lookup path is NO LONGER the live settlement path.
    The LIVE bot settles shotgun legs via settle_due_fires_polymarket(), which
    asks Polymarket how each bucket market resolved (the most faithful proxy for
    real paper-trading P&L). This function and the temperature-based
    settle_fire()/settle_due_fires() are retained ONLY for backtest-parity
    reconciliation (the research harness settles against a known daily-max). It
    stays stubbed (returns None) so the temperature path never fires in prod.

    TODO(wiring): not yet wired to a clean live source — returns None always.

    Why stubbed: the research harness (research_db/51_snapshot_backtest.py::
    fetch_resolution_truth) reads the truth from a DuckDB `weather_obs` table
    (source='historical_archive', obs_type='metar_hourly'), grouping MAX(temp_c)
    by city + *city-local* date. The live sqlite DB has no `weather_obs` table.
    Its closest analog is `metar_observations` (city, observed_at_utc,
    observed_temp_c), but it is NOT a clean drop-in:
      1. observed_at_utc is UTC while resolution_date is city-local — a correct
         daily-max requires converting each obs to the city's local tz before
         grouping by date. The city->tz map (CITY_TZ) lives only inside the
         research_db scripts and is not importable here.
      2. METAR collection is a shadow-only pipeline (never enabled in prod) with
         partial coverage, so naively taking MAX() risks settling a fire on an
         *incomplete* day and locking in a wrong outcome irreversibly.
      3. There is no "day complete" gate to confirm the local calendar day has
         finished and all hourly obs have landed before settling.
    A later wiring task will: import a shared city->tz map, convert
    observed_at_utc -> city-local date, gate on day-complete + obs coverage, then
    SELECT MAX(observed_temp_c)*9/5+32 for (city, local_date). Until then this
    returns None so settle_due_fires() safely skips every fire (no premature or
    wrong settlement). The two settle_due_fires tests inject their own truth_fn
    and do not depend on this function.
    """
    return None
