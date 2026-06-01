"""Tests for snapshot_cleanup.py.

Parquet retention is a simple N-day rolling window with a safety floor. Verify:
1. Files OLDER than retention → deleted
2. Files YOUNGER than retention → kept
3. Dry-run reports but does not delete
4. Safety floor caps deletion so the buffer can't be wiped (oldest deleted first)
5. No eligible files → no-op
Plus the SQLite prune (+VACUUM) deletes only rows older than retention.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture
def cleanup_module(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    parquet_dir = tmp_path / "snapshot_parquet"
    parquet_dir.mkdir()

    # Seed a SQLite table with a mix of old and new rows
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE bucket_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at_utc TEXT NOT NULL,
            event_slug TEXT NOT NULL,
            city TEXT NOT NULL,
            kind TEXT NOT NULL,
            resolution_date TEXT NOT NULL,
            sub_market_id TEXT NOT NULL,
            sub_market_condition_id TEXT NOT NULL,
            group_item_title TEXT NOT NULL,
            bucket_type TEXT NOT NULL,
            is_open_tail INTEGER NOT NULL DEFAULT 0
        );
    """)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    young = (now - timedelta(days=3)).isoformat()
    conn.executemany(
        "INSERT INTO bucket_snapshots(snapshot_at_utc, event_slug, city, kind, resolution_date, sub_market_id, sub_market_condition_id, group_item_title, bucket_type) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (old,   "e1", "nyc", "highest", "2026-05-09", "1", "0x1", "64-65°F", "range"),
            (old,   "e2", "nyc", "highest", "2026-05-09", "2", "0x2", "66-67°F", "range"),
            (young, "e3", "nyc", "highest", "2026-05-17", "3", "0x3", "64-65°F", "range"),
        ],
    )
    conn.commit()
    conn.close()

    import snapshot_cleanup
    monkeypatch.setattr(snapshot_cleanup, "DB_PATH", db_path)
    monkeypatch.setattr(snapshot_cleanup, "PARQUET_DIR", parquet_dir)
    # Low floor so the rolling-window tests don't trip the safety cap; one test
    # raises it explicitly to exercise the floor.
    monkeypatch.setattr(snapshot_cleanup, "PARQUET_MIN_KEEP_FILES", 1)
    return snapshot_cleanup, parquet_dir, db_path


def test_sqlite_cleanup_deletes_old_rows(cleanup_module):
    mod, _, db_path = cleanup_module
    n = mod.cleanup_sqlite(dry_run=False)
    assert n == 2  # two old rows
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert remaining == 1  # only the young row left


def test_sqlite_dry_run_does_not_delete(cleanup_module):
    mod, _, db_path = cleanup_module
    n = mod.cleanup_sqlite(dry_run=True)
    assert n == 2
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert remaining == 3  # nothing deleted


def _mkparquet(parquet_dir, days_ago):
    d = (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()
    f = parquet_dir / f"bucket_snapshots_{d}.parquet"
    f.touch()
    return f


def test_parquet_cleanup_deletes_older_than_retention(cleanup_module):
    mod, parquet_dir, _ = cleanup_module
    # RETENTION is 21 days. Two beyond it, one inside it.
    f_old1 = _mkparquet(parquet_dir, 40)
    f_old2 = _mkparquet(parquet_dir, 25)
    f_young = _mkparquet(parquet_dir, 10)

    n = mod.cleanup_vps_parquet(dry_run=False)

    assert n == 2
    assert not f_old1.exists()   # > 21d → deleted
    assert not f_old2.exists()   # > 21d → deleted
    assert f_young.exists()      # < 21d → kept


def test_parquet_dry_run_does_not_delete(cleanup_module):
    mod, parquet_dir, _ = cleanup_module
    # Two old + one young so the floor (1) never interferes with the old ones.
    f_old1 = _mkparquet(parquet_dir, 40)
    f_old2 = _mkparquet(parquet_dir, 30)
    f_young = _mkparquet(parquet_dir, 10)
    n = mod.cleanup_vps_parquet(dry_run=True)
    assert n == 2            # reports it would delete the two old ones
    assert f_old1.exists() and f_old2.exists() and f_young.exists()  # but didn't


def test_parquet_safety_floor_caps_deletion(cleanup_module, monkeypatch):
    # Even when every file is past retention, never drop below the floor.
    mod, parquet_dir, _ = cleanup_module
    monkeypatch.setattr(mod, "PARQUET_MIN_KEEP_FILES", 2)
    files = sorted(_mkparquet(parquet_dir, d) for d in (40, 35, 30, 25))  # all > 21d

    n = mod.cleanup_vps_parquet(dry_run=False)

    assert n == 2  # 4 files - floor of 2 = at most 2 deletable
    remaining = sorted(parquet_dir.glob("bucket_snapshots_*.parquet"))
    assert len(remaining) == 2
    # The two NEWEST survive (oldest deleted first).
    assert files[-1] in remaining and files[-2] in remaining


def test_parquet_no_eligible_files_is_noop(cleanup_module):
    mod, parquet_dir, _ = cleanup_module
    _mkparquet(parquet_dir, 5)
    _mkparquet(parquet_dir, 10)
    assert mod.cleanup_vps_parquet(dry_run=False) == 0
