"""Two-stage cleanup to bound disk usage on the VPS:

1. SQLite rows older than 3 UTC days: delete (per-day, committing between days)
   + VACUUM. We keep the last 3 days for the nightly rollup / live queries. (Was
   7 days until 2026-06-05; the VPS logs ~2 GB/day, so 7-day retention let the DB
   reach ~14 GB before the first prune — past the 20 GB disk wall.)
2. VPS parquet files >21 days old: delete (the VPS is a rolling buffer; the
   local archive is the permanent system of record).

History: the parquet stage was originally gated on a `.verified.<fname>` marker
living at an SSHFS mount of the local archive (LOCAL_ARCHIVE_MIRROR_PATH). That
mount was never set up on the VPS, so the gate skipped *every* file and parquets
accumulated at ~150 MB/day with no ceiling — the slow-fuse half of the 2026-06-01
disk-fill incident. Replaced with a simple N-day rolling window plus a safety
floor: never delete if it would leave fewer than PARQUET_MIN_KEEP_FILES recent
files (guards against a date-parse/clock bug nuking the whole buffer).

The local pull is currently manual (WinSCP). A scheduled local pull (workstream
"B3") is the real backstop for the 21-day window — until that lands, pull at
least every ~3 weeks or widen PARQUET_RETENTION_DAYS.

Run on the VPS via the snapshot_cleanup.timer (daily 02:00 UTC, after the 00:00
rollup).
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "weather_bot.db"
PARQUET_DIR = Path(__file__).parent.parent / "snapshot_parquet"

SQLITE_RETENTION_DAYS = 3
PARQUET_RETENTION_DAYS = 21
# Safety floor: refuse to prune parquets if doing so would leave fewer than this
# many files. A date-parse or system-clock bug must not be able to wipe the buffer.
PARQUET_MIN_KEEP_FILES = 7


def cleanup_sqlite(dry_run: bool) -> int:
    # Retain whole UTC days: keep today + (SQLITE_RETENTION_DAYS - 1) prior full days,
    # delete every UTC day strictly older than that. Working in whole days (not a
    # sub-day timestamp cutoff) matches the rollup's per-UTC-day grain and avoids a
    # boundary day straddling the cutoff — so the reported count always equals the
    # rows actually deleted.
    cutoff_date = (
        datetime.now(timezone.utc).date() - timedelta(days=SQLITE_RETENTION_DAYS)
    ).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    n_before = conn.execute(
        "SELECT COUNT(*) FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10) <= ?",
        [cutoff_date],
    ).fetchone()[0]
    if dry_run:
        print(f"[cleanup] DRY-RUN would delete {n_before} SQLite rows on/before {cutoff_date}")
        conn.close()
        return n_before

    # Delete one day at a time, committing between days. A single bulk
    # `DELETE ... <= cutoff` over a backlog can touch >1M rows, and its rollback
    # journal then needs scratch space the disk may not have — on 2026-06-05 exactly
    # that DELETE failed with SQLite error 13 ("disk full") and rolled back, leaving
    # the DB un-pruned and the disk filling. Per-day deletes bound each transaction's
    # journal to ~one day (~540k rows) and let a backlogged prune make forward
    # progress even under disk pressure.
    old_days = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT substr(snapshot_at_utc,1,10) AS d FROM bucket_snapshots "
            "WHERE substr(snapshot_at_utc,1,10) <= ? ORDER BY d",
            [cutoff_date],
        ).fetchall()
    ]
    for day in old_days:
        with conn:
            conn.execute(
                "DELETE FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10) = ?", [day]
            )

    # DELETE alone leaves freed pages in the file (the file never shrinks), so the
    # DB grows unbounded toward the disk ceiling even with daily pruning. VACUUM
    # returns those pages to the OS. With 3-day retention the live DB stays ~6 GB
    # (the VPS logs ~2 GB/day), so VACUUM's scratch requirement (~final DB size) is
    # manageable — unlike vacuuming a runaway 15 GB file, which needs 15 GB of free
    # disk it doesn't have. (Retention was 7 days until 2026-06-05; that let the DB
    # reach ~14 GB before the first prune, past the 20 GB disk wall.)
    if n_before:
        conn.execute("VACUUM")
    conn.close()
    print(f"[cleanup] deleted {n_before} SQLite rows on/before {cutoff_date} (VACUUM run)")
    return n_before


def cleanup_vps_parquet(dry_run: bool) -> int:
    """Delete VPS parquets older than PARQUET_RETENTION_DAYS, keeping the VPS as a
    rolling buffer. Honours a safety floor so a bug can't wipe the whole buffer."""
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=PARQUET_RETENTION_DAYS))

    dated_files = []
    for p in sorted(PARQUET_DIR.glob("bucket_snapshots_*.parquet")):
        try:
            fdate = datetime.strptime(p.stem.replace("bucket_snapshots_", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        dated_files.append((fdate, p))

    eligible = [(d, p) for (d, p) in dated_files if d < cutoff_date]
    if not eligible:
        return 0

    # Safety floor: never let pruning drop the buffer below the minimum file count.
    survivors_after = len(dated_files) - len(eligible)
    if survivors_after < PARQUET_MIN_KEEP_FILES:
        # Keep the newest eligible files until we'd retain the floor.
        eligible.sort()  # oldest first
        max_deletable = max(0, len(dated_files) - PARQUET_MIN_KEEP_FILES)
        if max_deletable < len(eligible):
            print(
                f"[cleanup] safety floor: capping parquet deletion at {max_deletable} "
                f"to retain {PARQUET_MIN_KEEP_FILES} files"
            )
        eligible = eligible[:max_deletable]

    deleted = 0
    for fdate, p in eligible:
        if dry_run:
            print(f"[cleanup] DRY-RUN would delete VPS parquet {p.name} (older than {cutoff_date})")
        else:
            p.unlink()
            print(f"[cleanup] deleted VPS parquet {p.name} (older than {cutoff_date})")
        deleted += 1
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
