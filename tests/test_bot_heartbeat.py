import json
from pathlib import Path

import health


def test_heartbeat_written_with_correct_fields(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=5, open_positions=2, balance=150.0)
    data = json.loads(hb_path.read_text())
    assert data["poll_count"] == 5
    assert data["open_positions"] == 2
    assert data["balance"] == 150.0
    assert "timestamp" in data


def test_crash_detection_stale_heartbeat(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    # Write a heartbeat with an old timestamp
    from datetime import datetime, timezone, timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    hb_path.write_text(json.dumps({
        "timestamp": old_ts,
        "poll_count": 100,
        "open_positions": 3,
        "balance": 200.0,
    }))
    result = health.check_crash(str(hb_path), stale_threshold=300)
    assert result is not None
    assert result["poll_count"] == 100
    assert result["minutes_ago"] >= 29


def test_crash_detection_fresh_heartbeat(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=10, open_positions=1, balance=100.0)
    result = health.check_crash(str(hb_path), stale_threshold=300)
    assert result is None


def test_bot_status_running(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=5, open_positions=0, balance=100.0)
    status = health.get_bot_status(str(hb_path), down_threshold=180)
    assert status["running"] is True


def test_bot_status_no_file(tmp_path):
    hb_path = tmp_path / "nonexistent.json"
    status = health.get_bot_status(str(hb_path), down_threshold=180)
    assert status["running"] is False
    assert status["no_file"] is True
