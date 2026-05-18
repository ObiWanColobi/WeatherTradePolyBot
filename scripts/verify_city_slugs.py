#!/usr/bin/env python3
"""
Verify Polymarket city → slug mapping for weather discovery.

For each city in WEATHER["top_cities"] (and the DB's observed list), probe
`/events/slug/highest-temperature-in-{candidate}-on-{month}-{day}-{year}` for
the next few days and pick the candidate that resolves first.

Outputs a Python dict literal you can paste into config.py as
WEATHER["city_slugs"].
"""
import sys
import time
import sqlite3
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, '.')
from config import WEATHER, DB_PATH, POLYMARKET_GAMMA_API

MONTHS = ['january','february','march','april','may','june','july',
          'august','september','october','november','december']


def candidate_slugs(city: str) -> list[str]:
    """Variants worth trying, in priority order."""
    c = city.lower().strip()
    base = c.replace(" ", "-")
    candidates = [base]
    # Known abbreviations / quirks
    if c == "new york city":
        candidates.insert(0, "nyc")
    if c == "los angeles":
        candidates.append("la")
    if c == "hong kong":
        candidates.append("hk")
    if c == "san francisco":
        candidates.append("sf")
    return list(dict.fromkeys(candidates))  # dedupe, preserve order


def probe(slug: str, date, kind: str = "highest") -> bool:
    event_slug = f"{kind}-temperature-in-{slug}-on-{MONTHS[date.month-1]}-{date.day}-{date.year}"
    try:
        r = requests.get(f"{POLYMARKET_GAMMA_API}/events/slug/{event_slug}", timeout=8)
        if r.status_code != 200:
            return False
        body = r.json()
        return bool(body and isinstance(body, dict) and body.get("markets"))
    except Exception:
        return False


def resolve_city(city: str, days_to_try: int = 4) -> str | None:
    today = datetime.now(timezone.utc).date()
    for cand in candidate_slugs(city):
        for delta in range(days_to_try + 1):
            d = today + timedelta(days=delta)
            for kind in ("highest", "lowest"):
                if probe(cand, d, kind):
                    return cand
            time.sleep(0.03)
    return None


def cities_from_db() -> list[str]:
    """Distinct cities observed in weather_city_log over the last 7 days."""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT DISTINCT lower(city) FROM weather_city_log "
            "WHERE logged_date >= date('now','-7 days')"
        ).fetchall()
        con.close()
        return sorted(set(r[0] for r in rows if r[0]))
    except Exception:
        return []


def main():
    config_cities = WEATHER.get("top_cities", [])
    db_cities = cities_from_db()
    all_cities = sorted(set(config_cities) | set(db_cities))
    print(f"Probing {len(all_cities)} cities ({len(config_cities)} from config + {len(db_cities)} from DB)\n")

    resolved = {}
    overrides = {}
    unresolved = []
    for city in all_cities:
        slug = resolve_city(city)
        if slug is None:
            unresolved.append(city)
            print(f"  {city:25s} -> UNRESOLVED")
            continue
        resolved[city] = slug
        default = city.lower().replace(" ", "-")
        marker = "" if slug == default else f"  (override: default would have been {default!r})"
        if slug != default:
            overrides[city] = slug
        print(f"  {city:25s} -> {slug}{marker}")

    print(f"\nResolved {len(resolved)}/{len(all_cities)} cities.")
    if overrides:
        print(f"\nNon-default mappings (paste into WEATHER['city_slugs']):")
        print("    \"city_slugs\": {")
        for c, s in sorted(overrides.items()):
            print(f'        "{c}": "{s}",')
        print("    },")
    if unresolved:
        print(f"\nUnresolved ({len(unresolved)}): {unresolved}")
        print("  These may be cities Polymarket no longer lists, or that haven't published")
        print("  markets in the probe window. The default `lower+hyphen` slug will be")
        print("  used and the helper will skip them on 404.")

    return 0 if not unresolved else 1


if __name__ == "__main__":
    sys.exit(main())
