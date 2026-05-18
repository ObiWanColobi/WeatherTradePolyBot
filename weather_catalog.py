#!/usr/bin/env python3
"""
Weather City Volume Catalog
────────────────────────────
Runs once daily (or on demand) to snapshot every active threshold weather
market's volume, price, and model probability into the weather_city_log table.

Over time this builds a historical record used to:
  - Rank cities by average 24h volume (replaces hardcoded top-city seed)
  - Track model accuracy per city (ensemble prob vs actual resolution)
  - Feed the decision layer with data-driven city tier assignments

Usage:
    python weather_catalog.py           # log today's snapshot and exit
    python weather_catalog.py --report  # print city volume/accuracy summary
"""
import argparse
import json
from datetime import datetime, timezone

import db
from layers.layer3_weather import WeatherLayer
from config import WEATHER
from markets.polymarket import get_resolution_status, iter_weather_markets

_layer = WeatherLayer()


# ── Market fetching (same slug filter as scanner) ─────────────────────────────

def _parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def fetch_all_weather_markets() -> list[dict]:
    """Fetch all threshold weather markets across the catalog city universe
    via deterministic slug discovery.

    Walks WEATHER["catalog_cities"] × forward 4 days × {highest, lowest},
    pulling event-level data from /events/slug/{slug}. Replaces the prior
    Gamma /markets index walk (broken since 2026-05-15 — see plan
    tasks/plans/2026-05-18_polymarket_slug_based_discovery.md).
    """
    cities = WEATHER.get("catalog_cities") or WEATHER.get("top_cities", [])
    markets = []

    for m in iter_weather_markets(cities, days_ahead=4):
        slug_check = (m.get("slug") or m.get("_event_slug") or "").lower()
        if "highest-temperature" not in slug_check and "lowest-temperature" not in slug_check:
            continue

        outcomes  = _parse_json_field(m.get("outcomes",      "[]"))
        prices    = _parse_json_field(m.get("outcomePrices", "[]"))
        token_ids = _parse_json_field(m.get("clobTokenIds",  "[]"))

        yes_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None
        )
        if yes_idx is None or yes_idx >= len(prices):
            continue

        try:
            price = float(prices[yes_idx])
        except (TypeError, ValueError):
            continue

        markets.append({
            "id":       m.get("conditionId") or m.get("id"),
            "question": m.get("question", ""),
            "token_id": token_ids[yes_idx] if yes_idx < len(token_ids) else None,
            "price":    price,
            "volume":   float(m.get("volume24hr") or 0),
            "end_date": m.get("_event_end") or m.get("endDateIso") or m.get("endDate") or "",
        })

    return markets


# ── Snapshot ──────────────────────────────────────────────────────────────────

def run_snapshot() -> int:
    """
    Fetch all active weather markets, compute model probability for each,
    and upsert into weather_city_log. Returns number of rows written.

    logged_date is the market's resolution day (parsed from end_date), not
    the snapshot date. This keeps the (logged_date, city, threshold) key
    pointing at the *same* underlying market across snapshots — so the
    backfill writes the resolution to the right row, and downstream readers
    like get_prev_day_resolution see the city's outcome on its named day.
    Markets without a parseable resolution date are skipped.
    """
    db.init_db()
    logged_at = datetime.now(timezone.utc).isoformat()
    today     = datetime.now(timezone.utc).date().isoformat()

    print(f"[catalog] Fetching markets at {logged_at}...")
    markets = fetch_all_weather_markets()
    print(f"[catalog] Found {len(markets)} weather markets. Computing model probs...")

    written = 0
    skipped = 0
    no_date = 0

    for market in markets:
        # Resolution date = first 10 chars of end_date_iso ('YYYY-MM-DD').
        # Without it we can't anchor the row to a settlement, so skip.
        market_resolution_date = (market.get("end_date") or "")[:10]
        if not market_resolution_date or len(market_resolution_date) != 10:
            no_date += 1
            continue

        scan_data = _layer.scan(market)
        if scan_data is None:
            skipped += 1
            continue

        if scan_data["market_type"] != "threshold":
            skipped += 1
            continue

        ens_n   = scan_data["ensemble_n"]
        ens_yes = scan_data["yes_ensemble"]
        ens_pct = (ens_yes / ens_n) if ens_n and ens_yes is not None else None

        entry = {
            "logged_date":  market_resolution_date,
            "city":         scan_data["city"],
            "market_type":  scan_data["market_type"],
            "threshold":    scan_data["target_str"],
            "volume_24h":   market["volume"],
            "yes_price":    market["price"],
            "model_prob":   scan_data["probability"],
            "ensemble_pct": ens_pct,
            "ensemble_n":   ens_n,
            "logged_at":    logged_at,
            "condition_id": market.get("id"),
        }

        db.upsert_city_log(entry)
        written += 1

    print(f"[catalog] Done — {written} rows written, {skipped} skipped, {no_date} no end_date.")
    return written


# ── Resolution backfill ───────────────────────────────────────────────────────

def backfill_resolutions(lookback_days: int = 14) -> dict:
    """Fill resolved_yes on past-day weather_city_log rows that have a
    condition_id. Calls CLOB /markets/<condition_id> per row; cheap because
    catalog rows are ~few-hundred and most resolve once then never re-checked.

    Skips rows where condition_id is NULL (pre-2026-05-11 historical rows).
    Skips rows whose markets aren't yet resolved (price mid-range).
    """
    db.init_db()

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT logged_date, city, threshold, condition_id
              FROM weather_city_log
             WHERE resolved_yes IS NULL
               AND condition_id IS NOT NULL
               AND logged_date <  DATE('now')
               AND logged_date >= DATE('now', '-{int(lookback_days)} days')
        """).fetchall()

    if not rows:
        print("[catalog] resolution backfill — nothing to do.")
        return {"updated": 0, "pending": 0, "errors": 0}

    updated = 0
    pending = 0
    errors  = 0

    for row in rows:
        try:
            res = get_resolution_status(row["condition_id"])
        except Exception as e:
            print(f"[catalog] backfill exception for {row['city']}/{row['logged_date']}: {e}")
            errors += 1
            continue

        if res is None:
            errors += 1
            continue
        if not res.get("resolved"):
            pending += 1
            continue

        yes_price = res.get("yes_price")
        if yes_price is None:
            pending += 1
            continue
        if yes_price >= 0.98:
            resolved_yes_bool = True
        elif yes_price <= 0.02:
            resolved_yes_bool = False
        else:
            pending += 1
            continue

        db.mark_city_log_resolved(
            row["logged_date"], row["city"], row["threshold"], resolved_yes_bool
        )
        updated += 1

    print(f"[catalog] resolution backfill — {updated} updated, "
          f"{pending} still-pending, {errors} errors (scanned {len(rows)}).")
    return {"updated": updated, "pending": pending, "errors": errors}


# ── Report ────────────────────────────────────────────────────────────────────

def run_report():
    """Print city volume and model accuracy summary from catalog data."""
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT
                city,
                COUNT(DISTINCT logged_date)         AS days_logged,
                ROUND(AVG(volume_24h), 0)           AS avg_vol_24h,
                ROUND(MAX(volume_24h), 0)           AS peak_vol_24h,
                COUNT(resolved_yes)                  AS resolved_count,
                ROUND(
                    100.0 * SUM(
                        CASE
                            WHEN resolved_yes = 1 AND model_prob >= 0.5 THEN 1
                            WHEN resolved_yes = 0 AND model_prob <  0.5 THEN 1
                            ELSE 0
                        END
                    ) / NULLIF(COUNT(resolved_yes), 0), 1
                )                                    AS model_accuracy_pct
            FROM weather_city_log
            GROUP BY city
            ORDER BY avg_vol_24h DESC
        """).fetchall()

    if not rows:
        print("[catalog] No data yet. Run without --report first to log a snapshot.")
        return

    print(f"\n{'City':<20} {'Days':>5} {'Avg Vol':>10} {'Peak Vol':>10} {'Resolved':>9} {'Accuracy':>9}")
    print("-" * 70)
    for r in rows:
        acc = f"{r['model_accuracy_pct']}%" if r["model_accuracy_pct"] is not None else "n/a"
        print(
            f"{r['city'].title():<20} "
            f"{r['days_logged']:>5} "
            f"${r['avg_vol_24h']:>9,.0f} "
            f"${r['peak_vol_24h']:>9,.0f} "
            f"{r['resolved_count']:>9} "
            f"{acc:>9}"
        )
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather city volume catalog")
    parser.add_argument("--report", action="store_true",
                        help="Print city volume/accuracy summary instead of logging")
    args = parser.parse_args()

    if args.report:
        run_report()
    else:
        run_snapshot()


if __name__ == "__main__":
    main()
