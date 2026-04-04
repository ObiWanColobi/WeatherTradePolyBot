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

import requests

import db
from layers.layer3_weather import WeatherLayer
from config import WEATHER

_session = requests.Session()
_session.headers.update({"User-Agent": "weather-catalog/1.0"})

GAMMA_API = "https://gamma-api.polymarket.com"
_layer    = WeatherLayer()


# ── Market fetching (same slug filter as scanner) ─────────────────────────────

def _parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def fetch_all_weather_markets() -> list[dict]:
    """Fetch all active threshold weather markets regardless of volume."""
    markets  = []
    per_page = 200

    for page in range(40):
        try:
            resp = _session.get(
                f"{GAMMA_API}/markets",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     per_page,
                    "offset":    page * per_page,
                    "order":     "volume24hr",
                    "ascending": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[catalog] fetch page {page} failed: {e}")
            break

        if not batch:
            break

        for m in batch:
            if "highest-temperature" not in (m.get("slug") or "").lower():
                continue

            outcomes  = _parse_json_field(m.get("outcomes",      "[]"))
            prices    = _parse_json_field(m.get("outcomePrices", "[]"))
            token_ids = _parse_json_field(m.get("clobTokenIds",  "[]"))

            yes_idx = next(
                (i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None
            )
            if yes_idx is None or yes_idx >= len(prices):
                continue

            price = float(prices[yes_idx])
            markets.append({
                "id":       m.get("conditionId") or m.get("id"),
                "question": m.get("question", ""),
                "token_id": token_ids[yes_idx] if yes_idx < len(token_ids) else None,
                "price":    price,
                "volume":   float(m.get("volume24hr") or 0),
                "end_date": m.get("endDateIso") or m.get("endDate") or "",
            })

        if batch and float(batch[-1].get("volume24hr") or 0) < 1:
            break
        if len(batch) < per_page:
            break

    return markets


# ── Snapshot ──────────────────────────────────────────────────────────────────

def run_snapshot() -> int:
    """
    Fetch all active weather markets, compute model probability for each,
    and upsert into weather_city_log. Returns number of rows written.
    """
    db.init_db()
    today    = datetime.now(timezone.utc).date().isoformat()
    logged_at = datetime.now(timezone.utc).isoformat()

    print(f"[catalog] Fetching markets for {today}...")
    markets = fetch_all_weather_markets()
    print(f"[catalog] Found {len(markets)} weather markets. Computing model probs...")

    written = 0
    skipped = 0

    for market in markets:
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
            "logged_date":  today,
            "city":         scan_data["city"],
            "market_type":  scan_data["market_type"],
            "threshold":    scan_data["target_str"],
            "volume_24h":   market["volume"],
            "yes_price":    market["price"],
            "model_prob":   scan_data["probability"],
            "ensemble_pct": ens_pct,
            "ensemble_n":   ens_n,
            "logged_at":    logged_at,
        }

        db.upsert_city_log(entry)
        written += 1

    print(f"[catalog] Done — {written} rows written, {skipped} skipped.")
    return written


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
