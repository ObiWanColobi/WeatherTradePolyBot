from unittest.mock import MagicMock, patch

import pytest
import requests

import config
import db
from snapshot_logger import fetch_active_weather_events, process_event_dict, run_one_poll


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


SAMPLE_EVENT = {
    "slug": "highest-temperature-in-nyc-on-may-20-2026",
    "endDate": "2026-05-20T12:00:00Z",
    "markets": [
        {
            "id": "12345",
            "conditionId": "0xabc",
            "groupItemTitle": "64-65°F",
            "bestBid": "0.30",
            "bestAsk": "0.35",
            "lastTradePrice": "0.32",
            "volume24hr": "523.4",
            "liquidityNum": "1200.5",
            "orderbook": None,
        },
        {
            "id": "12346",
            "conditionId": "0xdef",
            "groupItemTitle": "74°F or higher",
            "bestBid": "0.05",
            "bestAsk": "0.08",
            "lastTradePrice": "0.06",
            "volume24hr": "300",
            "liquidityNum": "800",
        },
    ],
}


def test_process_event_dict_yields_one_row_per_submarket():
    rows = list(process_event_dict(SAMPLE_EVENT, snapshot_at_utc="2026-05-19T20:00:00Z"))
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["event_slug"] == SAMPLE_EVENT["slug"]
    assert r0["city"] == "nyc"
    assert r0["kind"] == "highest"
    assert r0["resolution_date"] == "2026-05-20"
    assert r0["group_item_title"] == "64-65°F"
    assert r0["bound_lo_f"] == 64.0
    assert r0["bound_hi_f"] == 65.0
    assert r0["bucket_type"] == "range"
    assert r0["is_open_tail"] == 0
    assert r0["best_bid"] == 0.30
    assert r0["best_ask"] == 0.35
    assert abs(r0["mid_price"] - 0.325) < 0.001
    assert r0["volume_24h"] == 523.4

    r1 = rows[1]
    assert r1["bound_lo_f"] == 74.0
    assert r1["bound_hi_f"] is None
    assert r1["bucket_type"] == "tail"
    assert r1["is_open_tail"] == 1


def test_process_event_dict_skips_non_daily_temp_events():
    out = list(process_event_dict({"slug": "not-a-weather-event", "markets": []}, snapshot_at_utc="2026-05-19T20:00:00Z"))
    assert out == []


def test_run_one_poll_writes_to_db():
    with patch("snapshot_logger.fetch_active_weather_events", return_value=[SAMPLE_EVENT]):
        n = run_one_poll()
    assert n == 2

    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM bucket_snapshots ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["event_slug"] == SAMPLE_EVENT["slug"]
    assert rows[0]["city"] == "nyc"
    assert rows[0]["bound_lo_f"] == 64.0


def test_fetch_active_weather_events_raises_on_http_error():
    """Fetch errors must propagate so daemon's failure counter triggers."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("503 Server Error")

    with patch("snapshot_logger._session.get", return_value=mock_response):
        with pytest.raises(requests.HTTPError):
            fetch_active_weather_events()
