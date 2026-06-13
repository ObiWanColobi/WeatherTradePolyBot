"""Tests for snapshot_cleanup's VACUUM INTO + atomic-swap compaction (2026-06-13).

The bug: in-place VACUUM needs ~2× the DB size in scratch, which the 20 GB VPS
can't fit, so cleanup failed every night while rows piled up. The fix compacts via
VACUUM INTO (~1× scratch) and swaps. These tests prove the file actually shrinks,
data survives, and a failed compaction leaves the original untouched.
"""
import importlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _make_bloated_db(path: Path, keep_days: list[str], drop_days: list[str], rows_per_day=2000):
    """Build a bucket_snapshots DB, then DELETE the drop_days so the file has lots of
    freed (un-reclaimed) pages — i.e. on-disk size >> live data."""
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE bucket_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at_utc TEXT NOT NULL,
        payload TEXT)""")
    blob = "x" * 400   # fatten rows so the freelist is meaningful
    for day in keep_days + drop_days:
        conn.executemany(
            "INSERT INTO bucket_snapshots (snapshot_at_utc, payload) VALUES (?, ?)",
            [(f"{day}T12:00:00", blob) for _ in range(rows_per_day)])
    conn.commit()
    # delete the drop days -> leaves freed pages, file stays large
    for day in drop_days:
        conn.execute("DELETE FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10)=?", [day])
    conn.commit()
    conn.close()


def _load_cleanup(db_path: Path, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_DB_PATH", str(db_path))
    import snapshot_cleanup
    importlib.reload(snapshot_cleanup)
    return snapshot_cleanup


def test_compact_and_swap_shrinks_file_and_preserves_rows(tmp_path, monkeypatch):
    db = tmp_path / "snapshots.db"
    _make_bloated_db(db, keep_days=["2026-06-13"], drop_days=["2026-06-09", "2026-06-10"])
    size_before = db.stat().st_size
    rows_before = _count(db)

    cleanup = _load_cleanup(db, monkeypatch)
    cleanup._compact_and_swap()

    # file shrank (freed pages returned to OS), data intact, temp file gone
    assert db.stat().st_size < size_before
    assert _count(db) == rows_before
    assert sqlite3.connect(str(db)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (db.with_name(db.name + ".compact")).exists()


def test_compact_clears_a_stale_temp_first(tmp_path, monkeypatch):
    db = tmp_path / "snapshots.db"
    _make_bloated_db(db, keep_days=["2026-06-13"], drop_days=["2026-06-10"])
    # a leftover temp from a crashed prior run must not block VACUUM INTO
    stale = db.with_name(db.name + ".compact")
    stale.write_bytes(b"garbage from a crashed run")
    rows_before = _count(db)

    cleanup = _load_cleanup(db, monkeypatch)
    cleanup._compact_and_swap()

    assert _count(db) == rows_before
    assert not stale.exists()


def test_compaction_failure_leaves_original_intact(tmp_path, monkeypatch):
    db = tmp_path / "snapshots.db"
    _make_bloated_db(db, keep_days=["2026-06-13"], drop_days=["2026-06-10"])
    rows_before = _count(db)
    digest_before = db.read_bytes()

    cleanup = _load_cleanup(db, monkeypatch)
    # Force VACUUM INTO to blow up mid-way; the original DB must be untouched.
    monkeypatch.setattr(cleanup.sqlite3, "connect",
                        _failing_connect(cleanup.sqlite3, str(db)))
    cleanup._compact_and_swap()   # must NOT raise — failure is swallowed + logged

    # original file byte-identical, rows intact, no temp left behind
    assert db.read_bytes() == digest_before
    assert _count(db) == rows_before
    assert not (db.with_name(db.name + ".compact")).exists()


def test_full_cleanup_sqlite_prunes_and_compacts(tmp_path, monkeypatch):
    db = tmp_path / "snapshots.db"
    # old day present + recent day present; cleanup should delete old + shrink
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=10)).isoformat()
    recent = today.isoformat()
    _make_bloated_db(db, keep_days=[recent, old], drop_days=[], rows_per_day=1500)
    size_before = db.stat().st_size

    cleanup = _load_cleanup(db, monkeypatch)
    n = cleanup.cleanup_sqlite(dry_run=False)

    assert n == 1500                                   # the old day's rows deleted
    assert _count(db) == 1500                           # only recent remains
    assert db.stat().st_size < size_before              # compacted
    # old day truly gone
    left = sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM bucket_snapshots WHERE substr(snapshot_at_utc,1,10)=?", [old]
    ).fetchone()[0]
    assert left == 0


def _count(db: Path) -> int:
    c = sqlite3.connect(str(db))
    try:
        return c.execute("SELECT COUNT(*) FROM bucket_snapshots").fetchone()[0]
    finally:
        c.close()


def _failing_connect(sqlite_mod, db_path_str):
    """Return a sqlite3.connect replacement that raises when the script opens the
    SOURCE db for VACUUM INTO (first connect to db_path), simulating a disk-full."""
    real = sqlite_mod.connect
    state = {"hit": False}

    def fake(path, *a, **k):
        if str(path) == db_path_str and not state["hit"]:
            state["hit"] = True
            raise sqlite3.OperationalError("database or disk is full")
        return real(path, *a, **k)
    return fake
