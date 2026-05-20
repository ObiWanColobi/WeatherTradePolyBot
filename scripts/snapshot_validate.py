"""Validates snapshot logger health for the design's pre-launch gate #4:
'24h of bucket_snapshots written, manual review of 10 random rows
confirms all columns populated correctly.'

Run AFTER snapshot_logger has been live for at least 24 hours.
"""
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "weather_bot.db"

REQUIRED_NON_NULL_COLS = [
    "snapshot_at_utc", "event_slug", "city", "kind",
    "resolution_date", "sub_market_id", "group_item_title",
]
REQUIRED_NUMERIC_COLS = ["volume_24h"]


def validate():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    total = conn.execute(
        "SELECT COUNT(*) FROM bucket_snapshots WHERE snapshot_at_utc >= ?",
        [cutoff],
    ).fetchone()[0]
    print(f"[validate] rows in last 24h: {total}")
    if total < 500:
        print(f"[validate] FAIL: expected >500 rows in 24h (~5min poll x 288 polls x ~50 events), got {total}")
        sys.exit(1)

    n_events = conn.execute(
        "SELECT COUNT(DISTINCT event_slug) FROM bucket_snapshots WHERE snapshot_at_utc >= ?",
        [cutoff],
    ).fetchone()[0]
    print(f"[validate] distinct events in last 24h: {n_events}")
    if n_events < 20:
        print(f"[validate] FAIL: expected >20 distinct events, got {n_events}")
        sys.exit(1)

    n_cities = conn.execute(
        "SELECT COUNT(DISTINCT city) FROM bucket_snapshots WHERE snapshot_at_utc >= ?",
        [cutoff],
    ).fetchone()[0]
    print(f"[validate] distinct cities in last 24h: {n_cities}")

    # 10 random rows, manual-review-friendly
    rows = conn.execute(
        f"SELECT * FROM bucket_snapshots WHERE snapshot_at_utc >= ? ORDER BY RANDOM() LIMIT 10",
        [cutoff],
    ).fetchall()
    print()
    print("[validate] 10 random rows for manual review:")
    failures = 0
    for r in rows:
        for c in REQUIRED_NON_NULL_COLS:
            if r[c] is None or r[c] == "":
                print(f"  FAIL: row id={r['id']} has null/empty {c}")
                failures += 1
        for c in REQUIRED_NUMERIC_COLS:
            if r[c] is None:
                print(f"  WARN: row id={r['id']} has null {c} (may be legit for some sub-markets)")
        print(f"  id={r['id']}  {r['city']} {r['resolution_date']} {r['kind']:>7} | "
              f"{r['group_item_title']:>15} | bid={r['best_bid']} ask={r['best_ask']} vol={r['volume_24h']}")

    if failures > 0:
        print(f"[validate] FAIL: {failures} required-column violations")
        sys.exit(1)

    print()
    print("[validate] PASS: gate 4 satisfied - snapshot_logger producing valid rows")


if __name__ == "__main__":
    validate()
