"""Daily rollup: export previous day's bucket_snapshots rows to a parquet file on the VPS.

Idempotent: re-running on the same date overwrites the parquet (intentional — last write wins).
Does NOT delete from SQLite; that's snapshot_cleanup.py's job, gated on rsync verification.

Usage:
    python scripts/snapshot_rollup.py [YYYY-MM-DD]

If date omitted, defaults to (UTC today - 1 day).
"""
import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "weather_bot.db"
OUTPUT_DIR = Path(__file__).parent.parent / "snapshot_parquet"


def export_day(target_date: str) -> Path:
    """Export all bucket_snapshots rows where DATE(snapshot_at_utc) = target_date.
    Returns the path of the written parquet file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"bucket_snapshots_{target_date}.parquet"

    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT * FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10) = ?",
        conn,
        params=[target_date],
    )
    conn.close()

    if df.empty:
        print(f"[rollup] no rows for {target_date}")
        return out_path

    df.to_parquet(out_path, compression="zstd", index=False)
    h = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"[rollup] wrote {len(df)} rows for {target_date} -> {out_path}  sha256={h[:16]}")
    return out_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="YYYY-MM-DD; defaults to yesterday UTC")
    args = parser.parse_args()
    if args.date:
        target = args.date
    else:
        target = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    export_day(target)


if __name__ == "__main__":
    main()
