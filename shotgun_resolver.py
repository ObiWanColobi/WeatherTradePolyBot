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


def live_truth(city: str, resolution_date: str) -> float | None:
    """Observed daily-max °F for (city, resolution_date) from the live DB, or None
    if not yet known.

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
