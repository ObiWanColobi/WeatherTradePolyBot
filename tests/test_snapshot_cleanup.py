"""Tests for snapshot_cleanup.py.

Focus: the sentinel-gated deletion is load-bearing. Verify that:
1. Files OLDER than retention WITH sentinel → deleted
2. Files OLDER than retention WITHOUT sentinel → skipped (NOT deleted)
3. Files YOUNGER than retention → never touched (regardless of sentinel)
4. Dry-run does not actually delete anything
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
    mirror_dir = tmp_path / "local_archive_mirror"
    mirror_dir.mkdir()

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
    monkeypatch.setattr(snapshot_cleanup, "LOCAL_ARCHIVE_MIRROR_PATH", mirror_dir)
    return snapshot_cleanup, parquet_dir, mirror_dir, db_path


def test_sqlite_cleanup_deletes_old_rows(cleanup_module):
    mod, _, _, db_path = cleanup_module
    n = mod.cleanup_sqlite(dry_run=False)
    assert n == 2  # two old rows
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert remaining == 1  # only the young row left


def test_sqlite_dry_run_does_not_delete(cleanup_module):
    mod, _, _, db_path = cleanup_module
    n = mod.cleanup_sqlite(dry_run=True)
    assert n == 2
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    conn.close()
    assert remaining == 3  # nothing deleted


def test_parquet_cleanup_deletes_only_verified_old(cleanup_module):
    mod, parquet_dir, mirror_dir, _ = cleanup_module
    # Create three parquet files: old+verified, old+unverified, young+verified
    old_date_verified = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    old_date_unverified = (datetime.now(timezone.utc).date() - timedelta(days=35)).isoformat()
    young_date = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()

    f_old_ver = parquet_dir / f"bucket_snapshots_{old_date_verified}.parquet"
    f_old_unver = parquet_dir / f"bucket_snapshots_{old_date_unverified}.parquet"
    f_young = parquet_dir / f"bucket_snapshots_{young_date}.parquet"
    for f in (f_old_ver, f_old_unver, f_young):
        f.touch()

    # Drop sentinel markers for the verified ones
    (mirror_dir / f".verified.{f_old_ver.name}").touch()
    (mirror_dir / f".verified.{f_young.name}").touch()

    n = mod.cleanup_vps_parquet(dry_run=False)

    assert n == 1  # only the old+verified one was eligible
    assert not f_old_ver.exists()       # deleted
    assert f_old_unver.exists()         # skipped (no sentinel)
    assert f_young.exists()             # too young to touch


def test_parquet_dry_run_does_not_delete(cleanup_module):
    mod, parquet_dir, mirror_dir, _ = cleanup_module
    old_date = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    f = parquet_dir / f"bucket_snapshots_{old_date}.parquet"
    f.touch()
    (mirror_dir / f".verified.{f.name}").touch()
    mod.cleanup_vps_parquet(dry_run=True)
    assert f.exists()  # not actually deleted
