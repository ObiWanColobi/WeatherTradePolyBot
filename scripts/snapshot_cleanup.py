"""Two-stage cleanup, gated on backup verification:

1. SQLite rows >7 days old: delete (we keep last 7d for live queries)
2. VPS parquet files >30 days old: delete (we keep last 30d as insurance)

A VPS parquet file is eligible for deletion ONLY if the local archive
contains the corresponding .verified.<fname> marker — guaranteeing the
local backup is byte-identical to what we have on VPS.

Run on the VPS via daily cron AFTER snapshot_rollup.py + after local rsync
should have completed (e.g. 02:00 UTC if rollup is 00:00 and rsync 00:15).
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "weather_bot.db"
PARQUET_DIR = Path(__file__).parent.parent / "snapshot_parquet"

# These paths must match the local archive layout via SSHFS or shared mount.
# If local archive isn't accessible from VPS, this must be invoked from LOCAL after rsync.
LOCAL_ARCHIVE_MIRROR_PATH = Path("/mnt/local_archive_mirror/snapshot_parquet_local")

SQLITE_RETENTION_DAYS = 7
PARQUET_RETENTION_DAYS = 30


def cleanup_sqlite(dry_run: bool) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SQLITE_RETENTION_DAYS)).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    n_before = conn.execute(
        "SELECT COUNT(*) FROM bucket_snapshots WHERE snapshot_at_utc < ?", [cutoff]
    ).fetchone()[0]
    if dry_run:
        print(f"[cleanup] DRY-RUN would delete {n_before} SQLite rows older than {cutoff}")
        return n_before
    with conn:
        conn.execute("DELETE FROM bucket_snapshots WHERE snapshot_at_utc < ?", [cutoff])
    # DELETE alone leaves freed pages in the file (the file never shrinks), so the
    # DB grows unbounded toward the disk ceiling even with daily pruning. VACUUM
    # returns those pages to the OS. With 7-day retention the live DB stays ~1 GB,
    # so VACUUM's scratch requirement (~final DB size) is small and safe — unlike
    # vacuuming a runaway 15 GB file, which needs 15 GB of free disk it doesn't have.
    if n_before:
        conn.execute("VACUUM")
    conn.close()
    print(f"[cleanup] deleted {n_before} SQLite rows older than {cutoff} (VACUUM run)")
    return n_before


def cleanup_vps_parquet(dry_run: bool) -> int:
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=PARQUET_RETENTION_DAYS))
    deleted = 0
    skipped_unverified = 0
    for p in sorted(PARQUET_DIR.glob("bucket_snapshots_*.parquet")):
        try:
            fdate = datetime.strptime(p.stem.replace("bucket_snapshots_", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if fdate >= cutoff_date:
            continue
        marker = LOCAL_ARCHIVE_MIRROR_PATH / f".verified.{p.name}"
        if not marker.exists():
            print(f"[cleanup] SKIP {p.name}: no verified marker (local backup not confirmed)")
            skipped_unverified += 1
            continue
        if dry_run:
            print(f"[cleanup] DRY-RUN would delete VPS parquet {p.name}")
        else:
            p.unlink()
            print(f"[cleanup] deleted VPS parquet {p.name}")
        deleted += 1
    if skipped_unverified:
        print(f"[cleanup] WARNING: {skipped_unverified} files older than {cutoff_date} skipped — investigate rsync")
    return deleted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    n_sql = cleanup_sqlite(args.dry_run)
    n_par = cleanup_vps_parquet(args.dry_run)
    print(f"[cleanup] summary: SQLite={n_sql} rows, VPS parquet={n_par} files")


if __name__ == "__main__":
    main()
