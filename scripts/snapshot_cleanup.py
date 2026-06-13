"""Two-stage cleanup to bound disk usage on the VPS:

1. SQLite rows older than 3 UTC days: delete (per-day, committing between days)
   then compact via VACUUM INTO + atomic swap. We keep the last 3 days for the
   nightly rollup / live queries. (Was 7 days until 2026-06-05; the VPS logs
   ~2 GB/day, so 7-day retention let the DB reach ~14 GB before the first prune —
   past the 20 GB disk wall.)

   2026-06-13: switched the reclaim step from in-place `VACUUM` to `VACUUM INTO`
   + swap. In-place VACUUM needs ~2× the DB size in scratch (original + rebuilt
   copy + journal at once) — ~13 GB for a 6.4 GB DB — which the 20 GB box cannot
   fit, so it failed every night with "database or disk is full (13)" while the
   DELETE half kept working. VACUUM INTO writes one compacted copy (~1× scratch),
   which fits; we integrity-check it and os.replace() it over the original.
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
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Separate snapshot DB (see config.SNAPSHOT_DB_PATH). This script VACUUMs the
# whole file — it MUST target the snapshot DB, never the paper-trading DB.
DB_PATH = Path(os.environ.get(
    "SNAPSHOT_DB_PATH", str(Path(__file__).parent.parent / "snapshots.db")))
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
    conn.close()

    # DELETE alone leaves freed pages in the file (the file never shrinks), so the
    # DB grows unbounded toward the disk ceiling even with daily pruning. We must
    # return those pages to the OS — but NOT via in-place `VACUUM`: that holds the
    # original file + the full rebuilt copy + journal simultaneously (~2× the DB
    # size in scratch), which on 2026-06-13 failed every night with "database or
    # disk is full (13)" — a 6.4 GB DB needs ~13 GB free and the 20 GB box doesn't
    # have it. `VACUUM INTO` writes one compacted copy (~1× scratch) which fits, then
    # we atomically swap it in. On any failure the original is left untouched.
    if n_before:
        _compact_and_swap()
    print(f"[cleanup] deleted {n_before} SQLite rows on/before {cutoff_date} (compacted)")
    return n_before


def _compact_and_swap() -> None:
    """Reclaim freed pages by writing a compacted copy via VACUUM INTO and atomically
    swapping it over the live DB. Uses ~1× the DB size in scratch (vs ~2× for in-place
    VACUUM), so it fits on the disk-constrained VPS. Verifies integrity before the swap;
    leaves the original DB untouched on any error. See project_2026-06-13 disk-fill."""
    tmp = DB_PATH.with_name(DB_PATH.name + ".compact")
    # A stale temp from a crashed prior run would make VACUUM INTO fail (it refuses to
    # overwrite). Clear it first.
    if tmp.exists():
        tmp.unlink()
    try:
        src = sqlite3.connect(str(DB_PATH))
        try:
            # VACUUM INTO needs a literal path; quote-escape defensively.
            src.execute(f"VACUUM INTO '{str(tmp)}'")
        finally:
            src.close()

        # Verify the compacted copy before trusting it: integrity_check must be 'ok'
        # and the row count must match the source (guards a truncated/corrupt copy).
        chk = sqlite3.connect(str(tmp))
        try:
            integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
            n_compact = chk.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
        finally:
            chk.close()
        src2 = sqlite3.connect(str(DB_PATH))
        try:
            n_src = src2.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
        finally:
            src2.close()
        if integrity != "ok" or n_compact != n_src:
            raise RuntimeError(
                f"compacted copy rejected (integrity={integrity!r}, "
                f"rows {n_compact} vs source {n_src}) — keeping original")

        # Atomic on POSIX when src/dst share a filesystem (they do — same dir): the
        # canonical path is never left pointing at a half-written file.
        os.replace(str(tmp), str(DB_PATH))
        print(f"[cleanup] compacted {DB_PATH.name} ({n_compact} rows) via VACUUM INTO + swap")
    except Exception as e:
        # Never let a failed compaction take down the prune or corrupt the DB. The
        # DELETE already committed (retention is enforced); the file just won't shrink
        # this run — surface loudly and retry next night.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"[cleanup] WARNING: compaction failed, original DB intact: {e}")


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
