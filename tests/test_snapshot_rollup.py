"""Unit tests for the daily rollup script.

Uses a temp dir + temp sqlite DB so the test doesn't depend on (or pollute)
the production weather_bot.db.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

# Make the scripts/ directory importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture
def temp_db_and_dir(tmp_path, monkeypatch):
    """Set up a temp sqlite DB with a bucket_snapshots table and a temp output dir."""
    db_path = tmp_path / "test.db"
    out_dir = tmp_path / "parquet"
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
            is_open_tail INTEGER NOT NULL DEFAULT 0,
            bound_lo_f REAL,
            bound_hi_f REAL
        );
    """)
    conn.executemany(
        "INSERT INTO bucket_snapshots(snapshot_at_utc, event_slug, city, kind, resolution_date, sub_market_id, sub_market_condition_id, group_item_title, bucket_type) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("2026-05-20T01:00:00Z", "highest-temperature-in-nyc-on-may-20-2026", "nyc", "highest", "2026-05-20", "1", "0x1", "64-65°F", "range"),
            ("2026-05-20T01:05:00Z", "highest-temperature-in-nyc-on-may-20-2026", "nyc", "highest", "2026-05-20", "2", "0x2", "66-67°F", "range"),
            ("2026-05-21T01:00:00Z", "highest-temperature-in-nyc-on-may-21-2026", "nyc", "highest", "2026-05-21", "3", "0x3", "64-65°F", "range"),
        ],
    )
    conn.commit()
    conn.close()

    # Patch the module's paths
    import snapshot_rollup
    monkeypatch.setattr(snapshot_rollup, "DB_PATH", db_path)
    monkeypatch.setattr(snapshot_rollup, "OUTPUT_DIR", out_dir)
    return db_path, out_dir


def test_export_day_writes_parquet(temp_db_and_dir):
    _, out_dir = temp_db_and_dir
    import snapshot_rollup
    p = snapshot_rollup.export_day("2026-05-20")
    assert p.exists()
    df = pd.read_parquet(p)
    assert len(df) == 2
    assert set(df["snapshot_at_utc"]) == {"2026-05-20T01:00:00Z", "2026-05-20T01:05:00Z"}


def test_export_day_empty_returns_path_but_no_file(temp_db_and_dir):
    _, out_dir = temp_db_and_dir
    import snapshot_rollup
    p = snapshot_rollup.export_day("2030-01-01")
    # No rows for this date → no parquet written, but the path is returned for caller use
    assert not p.exists()


def test_export_day_idempotent_overwrites(temp_db_and_dir):
    _, out_dir = temp_db_and_dir
    import snapshot_rollup
    p1 = snapshot_rollup.export_day("2026-05-20")
    sha1 = p1.read_bytes()[:200]  # check first chunk
    p2 = snapshot_rollup.export_day("2026-05-20")
    sha2 = p2.read_bytes()[:200]
    assert p1 == p2
    assert sha1 == sha2  # deterministic re-export
