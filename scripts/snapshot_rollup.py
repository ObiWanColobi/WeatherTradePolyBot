"""Daily rollup: export previous day's bucket_snapshots rows to a parquet file on the VPS.

Idempotent: re-running on the same date overwrites the parquet (intentional — last write wins).
Does NOT delete from SQLite; that's snapshot_cleanup.py's job, gated on rsync verification.

Streams via chunked reads + pyarrow.ParquetWriter to bound memory usage. A full
day of bucket_snapshots loaded into a single DataFrame consumes 1-2 GB, which
OOM-kills the systemd unit on the 2 GB Kamatera VPS.

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

import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Separate snapshot DB (see config.SNAPSHOT_DB_PATH) — same file the logger writes.
DB_PATH = Path(os.environ.get(
    "SNAPSHOT_DB_PATH", str(Path(__file__).parent.parent / "snapshots.db")))
OUTPUT_DIR = Path(__file__).parent.parent / "snapshot_parquet"
CHUNK_SIZE = 50_000


def export_day(target_date: str) -> Path:
    """Export all bucket_snapshots rows where DATE(snapshot_at_utc) = target_date.
    Returns the path of the written parquet file (or the would-be path if empty).
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"bucket_snapshots_{target_date}.parquet"

    conn = sqlite3.connect(str(DB_PATH))
    chunks = pd.read_sql_query(
        "SELECT * FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10) = ?",
        conn,
        params=[target_date],
        chunksize=CHUNK_SIZE,
    )

    writer = None
    total_rows = 0
    try:
        for chunk in chunks:
            if chunk.empty:
                continue
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(out_path, table.schema, compression="zstd")
            writer.write_table(table)
            total_rows += len(chunk)
    finally:
        if writer is not None:
            writer.close()
        conn.close()

    if total_rows == 0:
        print(f"[rollup] no rows for {target_date}")
        return out_path

    h = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"[rollup] wrote {total_rows} rows for {target_date} -> {out_path}  sha256={h[:16]}")
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
