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
    young = (now - timedelta(days=1)).isoformat()  # well inside 3-day retention
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


def test_sqlite_retention_is_three_days(cleanup_module):
    """Retention shortened 7→3 days (disk-fill fix 2026-06-05). The live VPS DB
    grows ~2 GB/day; 7-day retention let it reach ~14 GB before the first prune,
    past the 20 GB disk wall. Three days keeps it ~6 GB."""
    mod, _, _ = cleanup_module
    assert mod.SQLITE_RETENTION_DAYS == 3


def _seed_days(db_path, day_offsets, rows_per_day=2):
    """Insert rows_per_day rows for each of the given day-offsets (days ago)."""
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc)
    for off in day_offsets:
        ts = (now - timedelta(days=off)).isoformat()
        rdate = (now - timedelta(days=off)).date().isoformat()
        conn.executemany(
            "INSERT INTO bucket_snapshots(snapshot_at_utc, event_slug, city, kind, resolution_date, sub_market_id, sub_market_condition_id, group_item_title, bucket_type) VALUES (?,?,?,?,?,?,?,?,?)",
            [(ts, f"e{off}_{i}", "nyc", "highest", rdate, str(i), f"0x{i}", "64-65°F", "range")
             for i in range(rows_per_day)],
        )
    conn.commit()
    conn.close()


def test_sqlite_cleanup_keeps_three_most_recent_days(cleanup_module):
    """Across a multi-day backlog, prune everything older than 3 days, keep the
    rest. Fresh fixture (drop the pre-seeded rows) so day math is unambiguous."""
    mod, _, db_path = cleanup_module
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM bucket_snapshots")
    conn.commit()
    conn.close()
    # 8-day backlog: offsets 0,1,2 are within 3-day retention (kept);
    # 3,4,5,6,7 are older (deleted). 5 days × 2 rows = 10 deleted, 6 kept.
    _seed_days(db_path, [0, 1, 2, 3, 4, 5, 6, 7], rows_per_day=2)

    n = mod.cleanup_sqlite(dry_run=False)

    assert n == 10
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    oldest = conn.execute("SELECT MIN(snapshot_at_utc) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert remaining == 6  # offsets 0,1,2 survive
    cutoff = (datetime.now(timezone.utc) - timedelta(days=mod.SQLITE_RETENTION_DAYS)).isoformat()
    assert oldest >= cutoff  # nothing older than retention survived


def test_sqlite_cleanup_reported_count_matches_actual_deletions(cleanup_module):
    """The printed/returned count must equal rows actually deleted — including on
    the boundary day. Per-day deletion works in whole UTC days, so the count must
    use the same whole-day predicate (not a sub-day timestamp cutoff), or it would
    undercount the partial boundary day it then deletes in full."""
    mod, _, db_path = cleanup_module
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM bucket_snapshots")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert before == 0
    _seed_days(db_path, [0, 1, 2, 4, 5, 6, 7], rows_per_day=3)

    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()

    n = mod.cleanup_sqlite(dry_run=False)

    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert n == total - remaining  # reported count == rows actually removed


def test_sqlite_cleanup_deletes_per_day_not_one_transaction(cleanup_module, monkeypatch):
    """The prune must delete day-by-day so a single transaction never needs more
    rollback-journal scratch than a near-full disk can give (the 2026-06-05 wall:
    a ~1M-row single DELETE failed with SQLite error 13 on a full disk). Assert
    the implementation issues one DELETE per distinct old day, committing between.
    """
    mod, _, db_path = cleanup_module
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM bucket_snapshots")
    conn.commit()
    conn.close()
    # Three distinct old days (offsets 5,6,7) → expect 3 separate day-scoped DELETEs.
    _seed_days(db_path, [0, 1, 5, 6, 7], rows_per_day=2)

    delete_statements = []
    real_connect = sqlite3.connect

    class _SpyConn:
        """Wraps a real connection, recording every DELETE issued through it."""
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("DELETE"):
                delete_statements.append(sql)
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    monkeypatch.setattr(mod.sqlite3, "connect", lambda *a, **k: _SpyConn(real_connect(*a, **k)))

    n = mod.cleanup_sqlite(dry_run=False)

    assert n == 6  # 3 old days × 2 rows
    # One DELETE per distinct old day — NOT a single bulk DELETE.
    assert len(delete_statements) == 3, (
        f"expected 3 per-day DELETEs, got {len(delete_statements)}"
    )


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
