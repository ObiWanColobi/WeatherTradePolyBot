import pytest
import config
import db


@pytest.fixture(autouse=True)
def use_memory_db(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", ":memory:")
    monkeypatch.setattr(db, "_DB_PATH", ":memory:")
    monkeypatch.setattr(db, "_MEM_CONN", None)
    db.init_db()
    yield


def test_bucket_snapshots_table_exists():
    conn = db.get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bucket_snapshots'"
    ).fetchone()
    assert row is not None, "bucket_snapshots table should exist after init_db()"


def test_bucket_snapshots_has_expected_columns():
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bucket_snapshots)").fetchall()}
    expected = {
        "id", "snapshot_at_utc", "event_slug", "event_end_iso",
        "condition_id", "city", "kind", "resolution_date",
        "sub_market_id", "sub_market_condition_id", "group_item_title",
        "bucket_type", "bound_lo_f", "bound_hi_f", "is_open_tail",
        "best_bid", "best_ask", "mid_price", "last_trade_price",
        "orderbook_bids_json", "orderbook_asks_json",
        "volume_24h", "liquidity_num", "raw_market_json"
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"
