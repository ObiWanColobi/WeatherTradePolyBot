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


def test_compaction_tolerates_concurrent_inserts_during_vacuum(tmp_path, monkeypatch):
    """The live snapshot_logger keeps INSERTing while VACUUM INTO runs (it takes
    minutes on the real DB). VACUUM INTO snapshots the source at its start, so the
    compacted copy legitimately has fewer rows than the source counted afterwards.
    The swap must still happen — a small shortfall from concurrent inserts is NOT
    corruption. (Regression for 2026-06-14..16: exact-equality check rejected three
    nights of good compactions, leaving the file un-shrunk.)"""
    db = tmp_path / "snapshots.db"
    _make_bloated_db(db, keep_days=["2026-06-13"], drop_days=["2026-06-10"])
    size_before = db.stat().st_size
    rows_before = _count(db)

    cleanup = _load_cleanup(db, monkeypatch)

    # Simulate the logger inserting a burst of rows after VACUUM INTO has written its
    # point-in-time copy but before the source is re-counted: the copy is short by
    # `burst` rows relative to the source, exactly like the live race. The source-count
    # connection is the LAST one opened (after src for VACUUM INTO and chk for the
    # copy), so we fire the burst just before that 3rd connection is handed back.
    burst = 25
    _patch_connect_to_inject_burst(cleanup, db, monkeypatch, before_connect_index=3, n_rows=burst)
    cleanup._compact_and_swap()   # must swap despite the count mismatch

    # The swap happened: file shrank and the post-VACUUM concurrent inserts survive
    # (they were committed to the original, which we keep if compaction is rejected;
    # but here compaction must be ACCEPTED, so the swapped-in copy lacks the burst —
    # what matters is the file compacted and stayed integrity-ok with the snapshot data).
    assert db.stat().st_size < size_before
    assert _count(db) >= rows_before        # snapshot rows preserved (burst may or may not be in copy)
    assert sqlite3.connect(str(db)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (db.with_name(db.name + ".compact")).exists()


def test_compaction_rejects_gross_row_shortfall(tmp_path, monkeypatch):
    """A truncated/corrupt copy (missing a large fraction of rows) must STILL be
    rejected — the tolerance only forgives a small concurrent-insert shortfall, not
    real data loss. Original DB left intact."""
    db = tmp_path / "snapshots.db"
    _make_bloated_db(db, keep_days=["2026-06-13"], drop_days=["2026-06-10"], rows_per_day=2000)

    cleanup = _load_cleanup(db, monkeypatch)

    # Balloon the source past the absolute tolerance floor right before the
    # source-count connection, so the compacted copy is short by far more than the
    # tolerance allows -> reject. (Larger than COMPACT_ROW_TOLERANCE = 50k.)
    gross = cleanup.COMPACT_ROW_TOLERANCE + 10_000
    _patch_connect_to_inject_burst(
        cleanup, db, monkeypatch, before_connect_index=3, n_rows=gross)
    cleanup._compact_and_swap()   # must NOT swap — shortfall is gross

    # Swap was rejected: the original (which received the gross burst) is still in
    # place, so its rows include the burst. A wrongful swap would have replaced it with
    # the point-in-time copy that LACKS the burst — that's the regression we're guarding.
    assert _count(db) >= gross
    assert not (db.with_name(db.name + ".compact")).exists()
    assert sqlite3.connect(str(db)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


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


def _patch_connect_to_inject_burst(cleanup, db, monkeypatch, before_connect_index, n_rows):
    """Wrap cleanup.sqlite3.connect so that, just before the Nth connection is handed
    back, a burst of rows is committed to the live DB on a side connection. Used to
    simulate the snapshot_logger writing concurrently while VACUUM INTO runs: the
    point-in-time compacted copy ends up short by `n_rows` vs the source counted after."""
    real_connect = cleanup.sqlite3.connect
    state = {"n": 0}

    def wrapped(path, *a, **k):
        state["n"] += 1
        if state["n"] == before_connect_index:
            side = real_connect(str(db))
            try:
                side.executemany(
                    "INSERT INTO bucket_snapshots (snapshot_at_utc, payload) VALUES (?, ?)",
                    [("2026-06-13T18:00:00", "y" * 400) for _ in range(n_rows)])
                side.commit()
            finally:
                side.close()
        return real_connect(path, *a, **k)

    monkeypatch.setattr(cleanup.sqlite3, "connect", wrapped)


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
