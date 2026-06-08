#!/usr/bin/env python3
"""Shotgun weather paper-trading bot.

Each poll:
  1. Resolve pass — settle fires whose bucket markets have resolved (Polymarket truth).
  2. Fire pass    — discover city-days; fire those in the 12h window (under exposure cap).
No early exits — every leg is held to its market's resolution.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import db
import health
import config
from config import SHOTGUN, TRADING_MODE, POLL_INTERVAL_SECONDS
from notifications import notify
from executor import create_executor
from shotgun_strategy import plan_fire, ShotgunConfig
from shotgun_discovery import discover_city_days
from shotgun.fire_window import in_fire_window, hours_to_close
import shotgun_resolver
from markets.open_meteo import get_ensemble_forecasts
from markets.polymarket import get_resolution_status
from layers.layer3_weather import CITY_COORDS

_executor = create_executor()


def _make_cfg() -> ShotgunConfig:
    return ShotgunConfig(
        mode=SHOTGUN["mode"], edge_threshold=SHOTGUN["edge_threshold"],
        mass_core_frac=SHOTGUN["mass_core_frac"], price_min=SHOTGUN["price_min"],
        price_max=SHOTGUN["price_max"], vol_min=SHOTGUN["vol_min"],
        sizing_mode=SHOTGUN["sizing_mode"], budget_per_city_day=SHOTGUN["budget_per_city_day"],
        per_bucket_liq_cap_frac=SHOTGUN["per_bucket_liq_cap_frac"],
    )


# Module-level seams (monkeypatchable in tests; real impls by default)
def _ensemble_fetch(lat, lon, tz):
    return get_ensemble_forecasts(lat, lon, tz)


def _resolution_fetch(condition_id):
    return get_resolution_status(condition_id)


_NYC_ALIAS = {"nyc": "new york city"}


def _coords_for(city: str):
    key = city.lower().replace("-", " ")
    key = _NYC_ALIAS.get(key, key)
    return CITY_COORDS.get(key)


def run_resolve_pass():
    settled = shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=_resolution_fetch)
    if settled:
        print(f"[bot] settled {settled} leg(s) via Polymarket resolution")


_VERBOSE = os.getenv("SHOTGUN_VERBOSE", "false").lower() == "true"


def run_fire_pass():
    balance = db.get_balance()
    cap = SHOTGUN["portfolio_exposure_cap_pct"] * balance
    already = db.get_fired_city_days()
    cfg = _make_cfg()
    groups = discover_city_days(SHOTGUN["cities"], days_ahead=SHOTGUN["discovery_days_ahead"])
    # Per-poll disposition: every reviewed city-day lands in exactly one bucket,
    # so an empty-looking poll still explains itself. counts -> one summary line;
    # SHOTGUN_VERBOSE=true also prints a line per city-day with its reason.
    counts = {"fired": 0, "already-fired": 0, "out-of-window": 0,
              "no-coords": 0, "no-edge": 0, "cap-skipped": 0, "error": 0}

    def _disp(reason, city, rd, detail=""):
        counts[reason] = counts.get(reason, 0) + 1
        if _VERBOSE:
            print(f"  [poll] {city:<14} {rd}  {reason}{('  ' + detail) if detail else ''}")

    capped = False
    for (city, rd), buckets in groups.items():
        if (city, rd) in already:
            _disp("already-fired", city, rd)
            continue
        if not in_fire_window(city, rd, SHOTGUN["fire_window_hours"]):
            h = hours_to_close(city, rd)
            _disp("out-of-window", city, rd, f"to_close={h:.1f}h" if h is not None else "")
            continue
        if capped or db.get_open_exposure() >= cap:
            capped = True   # once capped, every remaining in-window city-day is cap-skipped
            _disp("cap-skipped", city, rd)
            continue
        try:
            coords = _coords_for(city)
            if not coords:
                _disp("no-coords", city, rd)
                continue
            bets, center_f, density, rejected = plan_fire(
                city, rd, buckets, cfg, coords, _ensemble_fetch)
            if not bets:
                _disp("no-edge", city, rd, f"reviewed={len(rejected)}_filtered")
                continue
            lead = hours_to_close(city, rd) or 0.0
            fire_id = _executor.place_fire(
                city=city, resolution_date=rd, lead_hours=lead,
                center_f=center_f or 0.0, density_json=json.dumps(density or []),
                budget_usd=SHOTGUN["budget_per_city_day"], bets=bets,
                shadow_candidates=rejected)
            if fire_id:
                staked = sum(b["stake_usd"] for b in bets)
                _disp("fired", city, rd, f"{len(bets)}_legs shadow={len(rejected)}")
                print(f"[bot] FIRED {city} {rd} — {len(bets)} legs "
                      f"({len(rejected)} shadow) @ {lead:.1f}h to close")
                notify("info", "Shotgun Fire", f"{city} {rd}: {len(bets)} legs",
                       fields={"lead_h": f"{lead:.1f}", "staked": f"${staked:.2f}"})
        except Exception as e:
            _disp("error", city, rd, str(e))
            print(f"[bot] fire failed for {city} {rd} (non-fatal): {e}")
            continue

    nonzero = {k: v for k, v in counts.items() if v}
    summary = ", ".join(f"{k} {v}" for k, v in nonzero.items()) or "nothing reviewed"
    print(f"[bot] reviewed {len(groups)} city-days: {summary}")


def _dry_run_report():
    cfg = _make_cfg()
    groups = discover_city_days(SHOTGUN["cities"], days_ahead=SHOTGUN["discovery_days_ahead"])
    fired = db.get_fired_city_days()
    for (city, rd), buckets in groups.items():
        if (city, rd) in fired:
            continue
        h = hours_to_close(city, rd)
        win = in_fire_window(city, rd, SHOTGUN["fire_window_hours"])
        coords = _coords_for(city)
        n = 0
        n_shadow = 0
        if win and coords:
            bets, _, _, rejected = plan_fire(city, rd, buckets, cfg, coords, _ensemble_fetch)
            n = len(bets)
            n_shadow = len(rejected)
        h_str = f"{h:.1f}h" if h is not None else "n/a"
        print(f"  [dry] {city:<14} {rd}  to_close={h_str}  in_window={win}  "
              f"would_fire={n}_legs  shadow={n_shadow}")


def run(dry_run: bool = False):
    db.init_db()
    print(f"[bot] Shotgun paper trader — {'DRY-RUN' if dry_run else TRADING_MODE} mode")
    try:
        _executor.reconcile_positions()
    except Exception as e:
        print(f"[bot] reconcile failed (non-fatal): {e}")
    poll = 0
    hb = os.path.join(os.path.dirname(__file__), "heartbeat.json")
    while True:
        poll += 1
        print(f"\n{'-'*50}\n[bot] poll #{poll} {datetime.now(timezone.utc):%H:%M:%S} "
              f"balance=${db.get_balance():,.2f} open_fires={len(db.get_open_fires())}")
        try:
            run_resolve_pass()
            if dry_run:
                _dry_run_report()
            else:
                run_fire_pass()
        except Exception as e:
            print(f"[bot] poll error (non-fatal): {e}")
            try:
                notify("error", "Shotgun bot poll error", str(e))
            except Exception:
                pass
        try:
            health.write_heartbeat(hb, poll_count=poll,
                                   open_positions=len(db.get_open_bets()), balance=db.get_balance())
        except Exception as e:
            print(f"[bot] heartbeat failed (non-fatal): {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    ap = argparse.ArgumentParser(description="Shotgun weather paper trader")
    ap.add_argument("--dry-run", action="store_true", help="discover+plan, place nothing")
    run(dry_run=ap.parse_args().dry_run)


if __name__ == "__main__":
    main()
