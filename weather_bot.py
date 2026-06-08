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
from shotgun.forecast import members_f_for_date, build_density
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


def run_fire_pass():
    balance = db.get_balance()
    cap = SHOTGUN["portfolio_exposure_cap_pct"] * balance
    already = db.get_fired_city_days()
    cfg = _make_cfg()
    groups = discover_city_days(SHOTGUN["cities"], days_ahead=SHOTGUN["discovery_days_ahead"])
    for (city, rd), buckets in groups.items():
        if (city, rd) in already:
            continue
        if not in_fire_window(city, rd, SHOTGUN["fire_window_hours"]):
            continue
        if db.get_open_exposure() >= cap:
            print(f"[bot] exposure cap hit (${db.get_open_exposure():.0f}/${cap:.0f}) — skip {city} {rd}")
            break
        coords = _coords_for(city)
        if not coords:
            print(f"[bot] no coords for {city} — skip")
            continue
        bets = plan_fire(city, rd, buckets, cfg, coords, _ensemble_fetch)
        if not bets:
            continue
        lead = hours_to_close(city, rd) or 0.0
        ens = _ensemble_fetch(coords["lat"], coords["lon"], coords.get("tz", "auto"))
        center_f, density = build_density(members_f_for_date(ens, rd))
        fire_id = _executor.place_fire(
            city=city, resolution_date=rd, lead_hours=lead,
            center_f=center_f or 0.0, density_json=json.dumps(density or []),
            budget_usd=SHOTGUN["budget_per_city_day"], bets=bets)
        if fire_id:
            staked = sum(b["stake_usd"] for b in bets)
            print(f"[bot] FIRED {city} {rd} — {len(bets)} legs @ {lead:.1f}h to close")
            notify("info", "Shotgun Fire", f"{city} {rd}: {len(bets)} legs",
                   fields={"lead_h": f"{lead:.1f}", "staked": f"${staked:.2f}"})


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
        if win and coords:
            n = len(plan_fire(city, rd, buckets, cfg, coords, _ensemble_fetch))
        h_str = f"{h:.1f}h" if h is not None else "n/a"
        print(f"  [dry] {city:<14} {rd}  to_close={h_str}  in_window={win}  would_fire={n}_legs")


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
