import json
import time
from pathlib import Path
from unittest.mock import patch

import health


def test_write_heartbeat_creates_file(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=10, open_positions=3, balance=187.50)
    assert hb_path.exists()
    data = json.loads(hb_path.read_text())
    assert data["poll_count"] == 10
    assert data["open_positions"] == 3
    assert data["balance"] == 187.50
    assert "timestamp" in data


def test_write_heartbeat_overwrites_existing(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=1, open_positions=0, balance=100.0)
    health.write_heartbeat(str(hb_path), poll_count=2, open_positions=1, balance=99.0)
    data = json.loads(hb_path.read_text())
    assert data["poll_count"] == 2


def test_check_crash_no_file(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    result = health.check_crash(str(hb_path), stale_threshold=300)
    assert result is None  # first run, no alert


def test_check_crash_clean_restart(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=5, open_positions=0, balance=100.0)
    result = health.check_crash(str(hb_path), stale_threshold=300)
    assert result is None  # heartbeat is fresh, clean restart


def test_check_crash_stale_heartbeat(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    # Write a heartbeat with a timestamp 10 minutes ago
    from datetime import datetime, timezone, timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    data = {"timestamp": old_ts, "poll_count": 42, "open_positions": 2, "balance": 150.0}
    hb_path.write_text(json.dumps(data))

    result = health.check_crash(str(hb_path), stale_threshold=300)
    assert result is not None
    assert result["poll_count"] == 42
    assert result["minutes_ago"] >= 9  # ~10 min


def test_check_dashboard_bot_status_running(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=1, open_positions=0, balance=100.0)
    status = health.get_bot_status(str(hb_path), down_threshold=180)
    assert status["running"] is True


def test_check_dashboard_bot_status_down(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    from datetime import datetime, timezone, timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    data = {"timestamp": old_ts, "poll_count": 42, "open_positions": 0, "balance": 100.0}
    hb_path.write_text(json.dumps(data))
    status = health.get_bot_status(str(hb_path), down_threshold=180)
    assert status["running"] is False
    assert status["minutes_ago"] >= 4


def test_check_dashboard_bot_status_no_file(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    status = health.get_bot_status(str(hb_path), down_threshold=180)
    assert status["running"] is False
    assert status["no_file"] is True
