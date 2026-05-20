"""Tests for snapshot_validate.py.

The script calls sys.exit(1) on failure, so tests must catch SystemExit
and inspect the exit code rather than expect a return value.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def _seed_db(db_path: Path, n_rows: int, n_events: int = 5, n_cities: int = 5) -> None:
    """Insert n_rows recent (now - 1h) rows spread across n_events events."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE bucket_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at_utc TEXT NOT NULL,
            event_slug TEXT NOT NULL,
            event_end_iso TEXT,
            condition_id TEXT,
            city TEXT NOT NULL,
            kind TEXT NOT NULL,
            resolution_date TEXT NOT NULL,
            sub_market_id TEXT NOT NULL,
            sub_market_condition_id TEXT NOT NULL,
            group_item_title TEXT NOT NULL,
            bucket_type TEXT NOT NULL,
            bound_lo_f REAL,
            bound_hi_f REAL,
            is_open_tail INTEGER NOT NULL DEFAULT 0,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            last_trade_price REAL,
            orderbook_bids_json TEXT,
            orderbook_asks_json TEXT,
            volume_24h REAL,
            liquidity_num REAL,
            raw_market_json TEXT
        );
    """)
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    cities = [f"city{i}" for i in range(n_cities)]
    events = [f"highest-temperature-in-{cities[i % n_cities]}-on-may-{20 + i}-2026" for i in range(n_events)]
    rows = []
    for i in range(n_rows):
        rows.append((
            now.isoformat(),
            events[i % n_events],
            cities[i % n_cities],
            "highest",
            "2026-05-20",
            f"sm{i}",
            f"0x{i:x}",
            "64-65F",
            "range",
            500.0,
        ))
    conn.executemany(
        "INSERT INTO bucket_snapshots(snapshot_at_utc, event_slug, city, kind, resolution_date, sub_market_id, sub_market_condition_id, group_item_title, bucket_type, volume_24h) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def validate_mod(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    import snapshot_validate
    monkeypatch.setattr(snapshot_validate, "DB_PATH", db_path)
    return snapshot_validate, db_path


def test_validate_passes_with_enough_rows(validate_mod):
    mod, db_path = validate_mod
    _seed_db(db_path, n_rows=600, n_events=25, n_cities=10)
    # Should not raise / exit non-zero
    mod.validate()


def test_validate_fails_under_500_rows(validate_mod, capsys):
    mod, db_path = validate_mod
    _seed_db(db_path, n_rows=100)
    with pytest.raises(SystemExit) as e:
        mod.validate()
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "expected >500 rows" in out


def test_validate_fails_under_20_events(validate_mod):
    mod, db_path = validate_mod
    _seed_db(db_path, n_rows=600, n_events=10)  # plenty of rows but few events
    with pytest.raises(SystemExit) as e:
        mod.validate()
    assert e.value.code == 1
