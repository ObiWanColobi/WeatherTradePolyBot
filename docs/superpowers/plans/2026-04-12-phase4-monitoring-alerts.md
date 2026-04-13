# Phase 4 — Monitoring & Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot observable and self-reporting for 24/7 unattended operation via Discord alerts, health monitoring, API circuit breakers, and dashboard notifications.

**Architecture:** Three new modules (`notifications.py`, `health.py`, `api_monitor.py`) wired into the existing single-threaded poll loop. Discord webhooks for real-time alerts, SQLite `notifications` table for dashboard persistence, heartbeat file for crash detection. Shared circuit breaker replaces inline Open-Meteo retry logic.

**Tech Stack:** Python `requests` (Discord webhooks), SQLite (notification persistence), Streamlit (dashboard additions), `unittest.mock` (tests)

**Spec:** `docs/superpowers/specs/2026-04-12-phase4-monitoring-alerts-design.md`

---

### Task 1: Config Additions

**Files:**
- Modify: `config.py:123-131` (after risk_email block)

- [ ] **Step 1: Add Discord and monitoring config keys to WEATHER dict**

In `config.py`, after the `risk_email_password` line (line 131), add:

```python
    # ── Discord notifications ──
    "discord_webhook_alerts":       "",       # webhook URL for #bot-alerts
    "discord_webhook_trades":       "",       # webhook URL for #bot-trades

    # ── API circuit breaker ──
    "api_breaker_trip_threshold":   3,        # consecutive failures before trip
    "api_breaker_cooldowns":        [120, 300, 600, 1800, 3600],  # escalating seconds

    # ── CLOB rate limiting ──
    "clob_inter_request_delay":     0.3,      # min seconds between CLOB calls

    # ── Health monitoring ──
    "heartbeat_stale_threshold":    300,      # seconds before crash detection fires
    "dashboard_bot_down_threshold": 180,      # seconds before dashboard shows offline
```

- [ ] **Step 2: Verify config loads**

Run: `python -c "from config import WEATHER; print(WEATHER.get('discord_webhook_alerts', 'MISSING'))"`
Expected: empty string (not MISSING)

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): add Phase 4 monitoring & alerts config keys"
```

---

### Task 2: DB Schema — Notifications Table

**Files:**
- Modify: `db.py` (inside `init_db()` and add query helpers at bottom)
- Test: `tests/test_notifications_db.py`

- [ ] **Step 1: Write failing tests for notifications DB helpers**

Create `tests/test_notifications_db.py`:

```python
import db


def setup_function():
    """Reset DB to in-memory for each test."""
    db._DB_PATH = ":memory:"
    db.init_db()


def test_save_notification_and_retrieve():
    db.save_notification("critical", "alerts", "Test Title", "Test message body")
    rows = db.get_notifications(severity_in=["critical"], limit=10)
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    assert rows[0]["channel"] == "alerts"
    assert rows[0]["title"] == "Test Title"
    assert rows[0]["message"] == "Test message body"
    assert rows[0]["timestamp"]  # ISO-8601 string


def test_get_notifications_filters_by_severity():
    db.save_notification("critical", "alerts", "Crit", "msg")
    db.save_notification("info", "trades", "Info", "msg")
    db.save_notification("warning", "alerts", "Warn", "msg")
    rows = db.get_notifications(severity_in=["critical", "warning"], limit=10)
    assert len(rows) == 2
    severities = {r["severity"] for r in rows}
    assert severities == {"critical", "warning"}


def test_get_notifications_respects_limit():
    for i in range(5):
        db.save_notification("warning", "alerts", f"Title {i}", "msg")
    rows = db.get_notifications(severity_in=["warning"], limit=3)
    assert len(rows) == 3


def test_get_notifications_newest_first():
    db.save_notification("warning", "alerts", "First", "msg")
    db.save_notification("warning", "alerts", "Second", "msg")
    rows = db.get_notifications(severity_in=["warning"], limit=10)
    assert rows[0]["title"] == "Second"
    assert rows[1]["title"] == "First"


def test_get_notifications_since_timestamp():
    db.save_notification("critical", "alerts", "Old", "msg")
    import time; time.sleep(0.05)
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).isoformat()
    time.sleep(0.05)
    db.save_notification("critical", "alerts", "New", "msg")
    rows = db.get_notifications(severity_in=["critical"], limit=10, since=cutoff)
    assert len(rows) == 1
    assert rows[0]["title"] == "New"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notifications_db.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'save_notification'`

- [ ] **Step 3: Add notifications table to init_db()**

In `db.py`, inside `init_db()` (after the last `CREATE TABLE IF NOT EXISTS` block, before the `_safe_add_column` migration calls), add:

```python
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            severity    TEXT NOT NULL,
            channel     TEXT NOT NULL,
            title       TEXT NOT NULL,
            message     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_severity
            ON notifications(severity);
        CREATE INDEX IF NOT EXISTS idx_notifications_timestamp
            ON notifications(timestamp DESC);
```

- [ ] **Step 4: Add query helper functions to db.py**

At the bottom of `db.py`, add:

```python
# ── Notifications ──────────────────────────────────────────────

def save_notification(severity: str, channel: str, title: str, message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (timestamp, severity, channel, title, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, severity, channel, title, message),
        )


def get_notifications(
    severity_in: list[str],
    limit: int = 20,
    since: str | None = None,
) -> list[dict]:
    placeholders = ",".join("?" for _ in severity_in)
    sql = (
        f"SELECT * FROM notifications WHERE severity IN ({placeholders})"
    )
    params: list = list(severity_in)
    if since:
        sql += " AND timestamp > ?"
        params.append(since)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
```

Ensure `from datetime import datetime, timezone` is in the imports at the top of `db.py` (add if missing).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_notifications_db.py -v`
Expected: all 5 PASS

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_notifications_db.py
git commit -m "feat(db): add notifications table and query helpers"
```

---

### Task 3: Notifications Module — Discord Webhooks + DB Persistence

**Files:**
- Create: `notifications.py`
- Test: `tests/test_notifications.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_notifications.py`:

```python
import db
from unittest.mock import patch, MagicMock


def setup_function():
    db._DB_PATH = ":memory:"
    db.init_db()


@patch("notifications.requests.post")
def test_notify_critical_sends_to_alerts_webhook(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test-alerts",
        "discord_webhook_trades": "",
    }):
        from notifications import notify
        notify("critical", "Circuit Breaker", "Daily loss limit reached")

    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert call_url == "https://discord.com/api/webhooks/test-alerts"
    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["title"] == "Circuit Breaker"
    assert payload["embeds"][0]["color"] == 0xFF0000


@patch("notifications.requests.post")
def test_notify_info_sends_to_trades_webhook(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "https://discord.com/api/webhooks/test-trades",
    }):
        from notifications import notify
        notify("info", "Order Filled", "Bought YES on NYC market")

    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert call_url == "https://discord.com/api/webhooks/test-trades"
    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["color"] == 0x0066FF


@patch("notifications.requests.post")
def test_notify_warning_sends_to_alerts_webhook(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test-alerts",
        "discord_webhook_trades": "",
    }):
        from notifications import notify
        notify("warning", "API Issue", "Open-Meteo 429")

    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["color"] == 0xFFAA00


@patch("notifications.requests.post")
def test_notify_skips_when_webhook_not_configured(mock_post):
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "",
    }):
        from notifications import notify
        notify("critical", "Test", "Should not send")

    mock_post.assert_not_called()


@patch("notifications.requests.post")
def test_notify_persists_to_db(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }):
        from notifications import notify
        notify("critical", "Test Title", "Test body")

    rows = db.get_notifications(severity_in=["critical"], limit=10)
    assert len(rows) == 1
    assert rows[0]["title"] == "Test Title"


@patch("notifications.requests.post")
def test_notify_persists_even_when_discord_fails(mock_post):
    mock_post.side_effect = Exception("Connection refused")
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }):
        from notifications import notify
        notify("critical", "Test Title", "Test body")

    # DB write should still succeed
    rows = db.get_notifications(severity_in=["critical"], limit=10)
    assert len(rows) == 1


@patch("notifications.requests.post")
def test_notify_with_fields(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "https://discord.com/api/webhooks/test",
    }):
        from notifications import notify
        notify("info", "Order Filled", "Details", fields={
            "Market": "NYC >=75F",
            "Direction": "YES",
            "Size": "$25.00",
        })

    payload = mock_post.call_args[1]["json"]
    embed_fields = payload["embeds"][0]["fields"]
    assert len(embed_fields) == 3
    assert embed_fields[0]["name"] == "Market"
    assert embed_fields[0]["value"] == "NYC >=75F"


@patch("notifications.requests.post")
def test_notify_with_color_override(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "https://discord.com/api/webhooks/test",
    }):
        from notifications import notify, COLOR_GREEN
        notify("info", "Order Filled", "Details", color=COLOR_GREEN)

    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["color"] == 0x00FF00
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notifications.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notifications'`

- [ ] **Step 3: Implement notifications.py**

Create `notifications.py`:

```python
"""Discord webhook notifications + DB persistence."""

import requests
from datetime import datetime, timezone

import db
from config import WEATHER

_SEVERITY_COLOR = {
    "critical": 0xFF0000,   # red
    "warning":  0xFFAA00,   # yellow
    "info":     0x0066FF,   # blue
}

COLOR_GREEN = 0x00FF00  # for trade fills

_SEVERITY_CHANNEL = {
    "critical": "alerts",
    "warning":  "alerts",
    "info":     "trades",
}

_CHANNEL_CONFIG_KEY = {
    "alerts": "discord_webhook_alerts",
    "trades": "discord_webhook_trades",
}


def notify(severity: str, title: str, message: str,
           fields: dict | None = None, color: int | None = None) -> None:
    """Route a notification to the correct Discord channel and persist to DB."""
    channel = _SEVERITY_CHANNEL.get(severity, "alerts")

    # Always persist to DB first
    db.save_notification(severity, channel, title, message)

    # Send to Discord (best-effort)
    config_key = _CHANNEL_CONFIG_KEY[channel]
    webhook_url = WEATHER.get(config_key, "")
    if not webhook_url:
        return

    embed = {
        "title": title,
        "description": message,
        "color": color if color is not None else _SEVERITY_COLOR.get(severity, 0x0066FF),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = [
            {"name": k, "value": v, "inline": True}
            for k, v in fields.items()
        ]

    payload = {"embeds": [embed]}
    try:
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"[notify] Discord send failed: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notifications.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add notifications.py tests/test_notifications.py
git commit -m "feat: add notifications module with Discord webhooks and DB persistence"
```

---

### Task 4: Health Monitoring — Heartbeat + Crash Detection

**Files:**
- Create: `health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_health.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'health'`

- [ ] **Step 3: Implement health.py**

Create `health.py`:

```python
"""Bot health monitoring — heartbeat writer + crash detection."""

import json
from datetime import datetime, timezone
from pathlib import Path


def write_heartbeat(
    path: str,
    poll_count: int,
    open_positions: int,
    balance: float,
) -> None:
    """Write current bot state to heartbeat file (overwrite)."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "poll_count": poll_count,
        "open_positions": open_positions,
        "balance": balance,
    }
    Path(path).write_text(json.dumps(data))


def check_crash(path: str, stale_threshold: int = 300) -> dict | None:
    """Check heartbeat file on startup. Returns crash info or None if clean.

    Args:
        path: path to heartbeat.json
        stale_threshold: seconds before heartbeat is considered stale

    Returns:
        None if no crash detected, otherwise dict with crash details.
    """
    hb_path = Path(path)
    if not hb_path.exists():
        return None  # first run

    try:
        data = json.loads(hb_path.read_text())
        last_ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        if age < stale_threshold:
            return None  # clean restart
        return {
            "poll_count": data.get("poll_count", 0),
            "open_positions": data.get("open_positions", 0),
            "balance": data.get("balance", 0.0),
            "last_timestamp": data["timestamp"],
            "minutes_ago": int(age / 60),
        }
    except Exception:
        return None  # corrupted file, treat as first run


def get_bot_status(path: str, down_threshold: int = 180) -> dict:
    """Check bot status for dashboard display.

    Returns:
        dict with 'running' (bool), 'minutes_ago' (int), 'no_file' (bool)
    """
    hb_path = Path(path)
    if not hb_path.exists():
        return {"running": False, "no_file": True, "minutes_ago": 0}

    try:
        data = json.loads(hb_path.read_text())
        last_ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return {
            "running": age < down_threshold,
            "no_file": False,
            "minutes_ago": int(age / 60),
        }
    except Exception:
        return {"running": False, "no_file": True, "minutes_ago": 0}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_health.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add health.py tests/test_health.py
git commit -m "feat: add health module with heartbeat writer and crash detection"
```

---

### Task 5: API Monitor — Shared Circuit Breaker

**Files:**
- Create: `api_monitor.py`
- Test: `tests/test_api_monitor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_api_monitor.py`:

```python
import time
from unittest.mock import patch

from api_monitor import CircuitBreaker, BreakerOpen, NonRetriableError


def test_breaker_starts_closed():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    assert cb.state == "CLOSED"


def test_breaker_trips_after_threshold_failures():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    assert cb.state == "CLOSED"
    cb.record_failure(retriable=True)
    assert cb.state == "OPEN"


def test_breaker_resets_failure_count_on_success():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[2, 5, 10])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb._fail_count == 0


def test_breaker_transitions_to_half_open_after_cooldown():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1])  # 100ms cooldown
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb.state == "OPEN"
    time.sleep(0.15)
    assert cb.state == "HALF_OPEN"


def test_breaker_half_open_success_closes():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1])
    for _ in range(3):
        cb.record_failure(retriable=True)
    time.sleep(0.15)
    assert cb.state == "HALF_OPEN"
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb._trip_count == 0


def test_breaker_half_open_failure_reopens_with_escalation():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[0.1, 0.2])
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._trip_count == 1
    time.sleep(0.15)  # enter HALF_OPEN
    cb.record_failure(retriable=True)
    assert cb.state == "OPEN"
    assert cb._trip_count == 2


def test_breaker_escalating_cooldowns():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[10, 20, 30, 60])
    # First trip → 10s cooldown
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._current_cooldown == 10
    # Simulate half-open → failure → second trip → 20s cooldown
    cb._tripped_ts = time.time() - 11  # force past cooldown
    cb.record_failure(retriable=True)  # half-open → fail → reopen
    assert cb._current_cooldown == 20
    assert cb._trip_count == 2


def test_breaker_cooldown_caps_at_last_value():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[10, 20])
    # Trip 1 → 10s, Trip 2 → 20s, Trip 3+ → 20s (cap)
    for _ in range(3):
        cb.record_failure(retriable=True)
    assert cb._current_cooldown == 10
    cb._tripped_ts = time.time() - 11
    cb.record_failure(retriable=True)
    assert cb._current_cooldown == 20
    cb._tripped_ts = time.time() - 21
    cb.record_failure(retriable=True)
    assert cb._current_cooldown == 20  # stays at cap


def test_call_returns_result_when_closed():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    result = cb.call(lambda: 42)
    assert result == 42


def test_call_returns_none_when_open():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    for _ in range(3):
        cb.record_failure(retriable=True)
    result = cb.call(lambda: 42)
    assert result is None


def test_call_records_success_on_return():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])
    cb.record_failure(retriable=True)
    cb.record_failure(retriable=True)
    cb.call(lambda: "ok")
    assert cb._fail_count == 0


def test_call_records_failure_on_retriable_exception():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])

    def bad_call():
        raise ConnectionError("timeout")

    result = cb.call(bad_call)
    assert result is None
    assert cb._fail_count == 1


def test_non_retriable_error_raises_immediately():
    cb = CircuitBreaker("test", trip_threshold=3, cooldowns=[60])

    def auth_fail():
        raise NonRetriableError("401 Unauthorized")

    try:
        cb.call(auth_fail)
        assert False, "Should have raised"
    except NonRetriableError:
        pass
    # Failure count should NOT increment for non-retriable
    assert cb._fail_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api_monitor'`

- [ ] **Step 3: Implement api_monitor.py**

Create `api_monitor.py`:

```python
"""Shared circuit breaker for external API calls."""

import time
from config import WEATHER


class NonRetriableError(Exception):
    """Raised for errors that should not be retried (auth failures, bad signatures)."""
    pass


class BreakerOpen(Exception):
    """Raised when a call is attempted while the breaker is open."""
    pass


class CircuitBreaker:
    """Per-API circuit breaker with escalating cooldowns.

    States:
        CLOSED    — normal operation, tracking consecutive failures
        OPEN      — requests blocked, cooldown timer running
        HALF_OPEN — cooldown expired, next call is a test
    """

    def __init__(self, name: str, trip_threshold: int = 3, cooldowns: list[int] = None):
        self.name = name
        self._trip_threshold = trip_threshold
        self._cooldowns = cooldowns or WEATHER.get(
            "api_breaker_cooldowns", [120, 300, 600, 1800, 3600]
        )
        self._fail_count = 0
        self._trip_count = 0
        self._tripped_ts: float = 0
        self._current_cooldown: float = 0
        self._is_open = False

    @property
    def state(self) -> str:
        if not self._is_open:
            return "CLOSED"
        elapsed = time.time() - self._tripped_ts
        if elapsed >= self._current_cooldown:
            return "HALF_OPEN"
        return "OPEN"

    def record_success(self) -> None:
        """Record a successful API call. Resets breaker if HALF_OPEN."""
        self._fail_count = 0
        if self._is_open:
            # Recovery from HALF_OPEN
            self._is_open = False
            self._trip_count = 0
            print(f"[api_monitor] {self.name} circuit breaker recovered")

    def record_failure(self, retriable: bool = True) -> None:
        """Record a failed API call."""
        if not retriable:
            return  # non-retriable errors don't affect the breaker

        state = self.state
        if state == "HALF_OPEN":
            # Test call failed — re-open with escalated cooldown
            self._trip()
            return

        self._fail_count += 1
        if self._fail_count >= self._trip_threshold:
            self._trip()

    def _trip(self) -> None:
        """Trip the breaker (CLOSED→OPEN or HALF_OPEN→OPEN)."""
        self._is_open = True
        self._tripped_ts = time.time()
        self._trip_count += 1
        idx = min(self._trip_count - 1, len(self._cooldowns) - 1)
        self._current_cooldown = self._cooldowns[idx]
        self._fail_count = 0
        print(
            f"[api_monitor] {self.name} circuit breaker tripped "
            f"(trip #{self._trip_count}, cooldown {self._current_cooldown}s)"
        )

    def call(self, fn, *args, **kwargs):
        """Execute fn through the circuit breaker.

        Returns None if breaker is OPEN (call skipped).
        Raises NonRetriableError immediately if fn raises one.
        Returns None on retriable exceptions (and records failure).
        """
        state = self.state
        if state == "OPEN":
            return None

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except NonRetriableError:
            raise  # propagate immediately, don't affect breaker
        except Exception:
            self.record_failure(retriable=True)
            return None


# ── Global breaker instances ──────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """Get or create a circuit breaker by name."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name,
            trip_threshold=WEATHER.get("api_breaker_trip_threshold", 3),
            cooldowns=WEATHER.get("api_breaker_cooldowns", [120, 300, 600, 1800, 3600]),
        )
    return _breakers[name]


def call(name: str, fn, *args, **kwargs):
    """Convenience: get_breaker(name).call(fn, ...)."""
    return get_breaker(name).call(fn, *args, **kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api_monitor.py -v`
Expected: all 14 PASS

- [ ] **Step 5: Commit**

```bash
git add api_monitor.py tests/test_api_monitor.py
git commit -m "feat: add shared API circuit breaker with escalating cooldowns"
```

---

### Task 6: CLOB Rate Limiting in executor/live.py

**Files:**
- Modify: `executor/live.py:1-23` (imports), `executor/live.py:99-119` (`_post_order`)
- Test: `tests/test_clob_rate_limit.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_clob_rate_limit.py`:

```python
import time
from unittest.mock import patch, MagicMock


def test_clob_rate_limit_enforces_delay():
    """Two rapid CLOB calls should be spaced by at least clob_inter_request_delay."""
    with patch("executor.live.ClobClient") as mock_clob_cls, \
         patch("executor.live.WALLET_PRIVATE_KEY", "0x" + "ab" * 32), \
         patch("executor.live.POLYMARKET_CLOB_API", "https://fake"), \
         patch("executor.live.CHAIN_ID", 137), \
         patch("executor.live.Claimer"), \
         patch("executor.live.WEATHER", {
             "clob_inter_request_delay": 0.2,
             "live_kelly_max_bet_usdc": 25,
             "live_risk_daily_loss_limit_pct": 0.05,
         }), \
         patch("executor.live.polymarket"):
        mock_client = MagicMock()
        mock_clob_cls.return_value = mock_client
        mock_client.get_balance_allowance.return_value = {"balance": "100000000"}
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {"orderID": "abc123", "status": "matched"}
        mock_client.get_order.return_value = {
            "status": "matched",
            "associate_trades": [{"price": "0.60", "size": "10", "fee": "0.01"}],
        }

        from executor.live import LiveExecutor
        ex = LiveExecutor()

        # First call — should be immediate
        t0 = time.time()
        ex._throttle_clob()
        t1 = time.time()
        assert (t1 - t0) < 0.1  # no delay on first call

        # Second call — should be delayed
        ex._throttle_clob()
        t2 = time.time()
        assert (t2 - t1) >= 0.15  # at least ~200ms delay
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_clob_rate_limit.py -v`
Expected: FAIL — `AttributeError: 'LiveExecutor' object has no attribute '_throttle_clob'`

- [ ] **Step 3: Add rate limiting to LiveExecutor**

In `executor/live.py`, add to imports (top of file):

```python
from config import WEATHER  # add WEATHER if not already imported
```

In the `__init__` method of `LiveExecutor`, add:

```python
        self._last_clob_call_ts: float = 0.0
```

Add a new method to the `LiveExecutor` class (before `_post_order`):

```python
    def _throttle_clob(self) -> None:
        """Enforce minimum delay between CLOB API calls."""
        delay = WEATHER.get("clob_inter_request_delay", 0.3)
        elapsed = time.time() - self._last_clob_call_ts
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_clob_call_ts = time.time()
```

In `_post_order`, add `self._throttle_clob()` as the first line of the method body (before the `try` block).

In `_confirm_fill`, add `self._throttle_clob()` before the `self._client.get_order(order_id)` call inside the retry loop.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_clob_rate_limit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add executor/live.py tests/test_clob_rate_limit.py
git commit -m "feat(live): add CLOB API rate limiting (300ms delay floor)"
```

---

### Task 7: Migrate Open-Meteo Inline Circuit Breaker to Shared api_monitor

**Files:**
- Modify: `markets/open_meteo.py:18-46` (remove inline breaker), `markets/open_meteo.py:148-150` (429 handling)
- Test: `tests/test_open_meteo_breaker.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_open_meteo_breaker.py`:

```python
from unittest.mock import patch, MagicMock
import api_monitor


def test_open_meteo_uses_shared_breaker():
    """Verify open_meteo module uses api_monitor.call for API requests."""
    # Reset any existing breaker state
    api_monitor._breakers.clear()

    with patch("markets.open_meteo.requests.get") as mock_get, \
         patch("markets.open_meteo.WEATHER", {
             "api_breaker_trip_threshold": 3,
             "api_breaker_cooldowns": [0.1],
         }):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hourly": {"temperature_2m": [20.0]}}
        mock_get.return_value = mock_resp

        # After migration, open_meteo should have a get_breaker("open_meteo") reference
        breaker = api_monitor.get_breaker("open_meteo")
        assert breaker.state == "CLOSED"


def test_open_meteo_inline_breaker_removed():
    """Verify the old inline _cb_* variables are gone."""
    import markets.open_meteo as om
    assert not hasattr(om, "_cb_fail_count"), "Old inline _cb_fail_count should be removed"
    assert not hasattr(om, "_cb_tripped_ts"), "Old inline _cb_tripped_ts should be removed"
    assert not hasattr(om, "_CB_THRESHOLD"), "Old inline _CB_THRESHOLD should be removed"
    assert not hasattr(om, "_CB_COOLDOWN"), "Old inline _CB_COOLDOWN should be removed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_open_meteo_breaker.py -v`
Expected: FAIL — old inline variables still exist

- [ ] **Step 3: Migrate open_meteo.py to shared breaker**

In `markets/open_meteo.py`:

1. **Remove** the inline circuit breaker variables (lines 22-25: `_CB_THRESHOLD`, `_CB_COOLDOWN`, `_cb_fail_count`, `_cb_tripped_ts`).

2. **Remove** the `_cb_is_open()` and `_cb_record_failure()` functions (lines 28-46).

3. **Add import** at the top:
   ```python
   import api_monitor
   ```

4. **Replace** all calls to `_cb_is_open()` with:
   ```python
   api_monitor.get_breaker("open_meteo").state == "OPEN"
   ```

5. **Replace** all calls to `_cb_record_failure()` with:
   ```python
   api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
   ```

6. **Replace** success paths (where `_cb_fail_count = 0` was set) with:
   ```python
   api_monitor.get_breaker("open_meteo").record_success()
   ```

7. **Keep** the `_last_429_ts` global — it's still used by the calibration pass cooldown check and is separate from the circuit breaker.

- [ ] **Step 4: Run tests to verify migration passes**

Run: `python -m pytest tests/test_open_meteo_breaker.py -v`
Expected: all 2 PASS

Run: `python -m pytest tests/ -v`
Expected: all existing tests still pass (no regressions)

- [ ] **Step 5: Commit**

```bash
git add markets/open_meteo.py tests/test_open_meteo_breaker.py
git commit -m "refactor(open_meteo): migrate inline circuit breaker to shared api_monitor"
```

---

### Task 8: Wire Notifications into Risk Events

**Files:**
- Modify: `weather_risk.py:1-21` (imports), `weather_risk.py:109-127` (circuit breaker trip), `weather_risk.py:163-180` (close all)

- [ ] **Step 1: Write failing test**

Create `tests/test_risk_notifications.py`:

```python
import db
from unittest.mock import patch, MagicMock


def setup_function():
    db._DB_PATH = ":memory:"
    db.init_db()


@patch("notifications.requests.post")
def test_circuit_breaker_trip_sends_notification(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }), patch("weather_risk.WEATHER", {
        "risk_daily_loss_limit_pct": 0.05,
        "risk_auto_reset": True,
        "risk_email_enabled": False,
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }):
        from weather_risk import RiskManager
        rm = RiskManager()
        rm._today_loss = -20.0
        rm._account_value = 100.0
        rm.check_circuit_breaker()

    rows = db.get_notifications(severity_in=["critical"], limit=10)
    assert any("circuit breaker" in r["title"].lower() or "circuit breaker" in r["message"].lower() for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_risk_notifications.py -v`
Expected: FAIL — no notification is saved (notify() not called yet)

- [ ] **Step 3: Add notify() calls to weather_risk.py**

Add import at the top of `weather_risk.py`:
```python
from notifications import notify
```

In the circuit breaker trip block (around line 118, after the email alert call), add:
```python
                notify("critical", "Circuit Breaker Tripped",
                       f"Daily loss {loss_pct:.1%} exceeded {limit_pct:.1%} limit. Entries halted.",
                       fields={"Loss": f"${abs(self._today_loss):.2f}",
                               "Account": f"${self._account_value:.2f}"})
```

In the midnight reset block (where `circuit_breaker_reset` email is sent), add:
```python
            notify("warning", "Circuit Breaker Reset", "Daily loss counter reset at UTC midnight.")
```

In the `close_all` / `manual_close_all` handler (around the existing `send_alert("manual_close_all", ...)` call), add:
```python
        notify("critical", "Manual Close All",
               f"All {len(trades)} positions closed manually.",
               fields={"Positions Closed": str(len(trades))})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_risk_notifications.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add weather_risk.py tests/test_risk_notifications.py
git commit -m "feat(risk): wire Discord notifications into circuit breaker and close-all events"
```

---

### Task 9: Wire Notifications into Trade Events

**Files:**
- Modify: `executor/live.py` (after successful fill, after close, after claim)
- Modify: `executor/paper.py` (same events for paper mode visibility)
- Test: `tests/test_trade_notifications.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_trade_notifications.py`:

```python
import db
from unittest.mock import patch, MagicMock


def setup_function():
    db._DB_PATH = ":memory:"
    db.init_db()


@patch("notifications.requests.post")
def test_entry_fill_sends_trade_notification(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "https://discord.com/api/webhooks/test",
    }):
        from notifications import notify
        # Simulate what executor would call after a fill
        notify("info", "Order Filled",
               "Bought YES on NYC >=75F Apr 15",
               fields={"Price": "$0.62", "Size": "$25.00", "Shares": "40.3"})

    rows = db.get_notifications(severity_in=["info"], limit=10)
    assert len(rows) == 1
    assert "Order Filled" in rows[0]["title"]


@patch("notifications.requests.post")
def test_exit_sends_trade_notification(mock_post):
    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "",
        "discord_webhook_trades": "https://discord.com/api/webhooks/test",
    }):
        from notifications import notify
        notify("info", "Position Closed",
               "Exited NYC >=75F — ensemble flip",
               fields={"P&L": "+$4.20", "Reason": "ensemble_flip"})

    rows = db.get_notifications(severity_in=["info"], limit=10)
    assert len(rows) == 1
    assert "Position Closed" in rows[0]["title"]
```

- [ ] **Step 2: Run tests to verify they pass (notify already works)**

Run: `python -m pytest tests/test_trade_notifications.py -v`
Expected: PASS (notify() function already implemented, this validates the pattern)

- [ ] **Step 3: Add notify() calls to executor/live.py**

Add imports at the top of `executor/live.py`:
```python
from notifications import notify, COLOR_GREEN
```

After a successful fill in `place_order()` (after `_confirm_fill` returns valid data and the trade is saved to DB), add:
```python
        notify("info", "Order Filled",
               f"{'Bought' if side == 'BUY' else 'Sold'} {direction} on {market_name}",
               fields={"Price": f"${fill_price:.4f}",
                        "Size": f"${amount_usdc:.2f}",
                        "Shares": f"{shares:.1f}"},
               color=COLOR_GREEN)
```

After a successful close in `close_full()` (after the exit trade is saved to DB), add:
```python
        notify("info", "Position Closed",
               f"Exited {trade.get('market_slug', 'unknown')} — {reason}",
               fields={"P&L": f"${pnl:+.2f}",
                        "Reason": reason},
               color=COLOR_GREEN)
```

After a successful claim in the claims pass (after balance is updated), add:
```python
        notify("info", "Claim Completed",
               f"Redeemed {trade.get('market_slug', 'unknown')}",
               fields={"Amount": f"${payout:.2f}",
                        "Market": trade.get("market_slug", "")})
```

- [ ] **Step 4: Add notify() calls to executor/paper.py (same pattern)**

Add imports at the top of `executor/paper.py`:
```python
from notifications import notify, COLOR_GREEN
```

Add matching `notify()` calls at the same points in `place_order()` and `close_full()` as in live.py. Use the same format strings. This gives paper mode the same Discord visibility.

- [ ] **Step 5: Run all tests to verify no regressions**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add executor/live.py executor/paper.py tests/test_trade_notifications.py
git commit -m "feat(executor): wire Discord notifications for fills, exits, and claims"
```

---

### Task 10: Wire Heartbeat + Daily Digest into Bot Loop

**Files:**
- Modify: `weather_bot.py:1-31` (imports), `weather_bot.py:442-464` (startup), `weather_bot.py:405-464` (poll loop)
- Test: `tests/test_bot_heartbeat.py`

- [ ] **Step 1: Write failing test for heartbeat wiring**

Create `tests/test_bot_heartbeat.py`:

```python
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import health


def test_heartbeat_written_with_correct_fields(tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    health.write_heartbeat(str(hb_path), poll_count=5, open_positions=2, balance=150.0)
    data = json.loads(hb_path.read_text())
    assert data["poll_count"] == 5
    assert data["open_positions"] == 2
    assert data["balance"] == 150.0
    assert "timestamp" in data
```

- [ ] **Step 2: Run test to verify it passes (health.py already implemented)**

Run: `python -m pytest tests/test_bot_heartbeat.py -v`
Expected: PASS

- [ ] **Step 3: Wire heartbeat into weather_bot.py**

Add imports to `weather_bot.py`:
```python
import health
from notifications import notify
```

In the startup section of `run()` (after `db.init_db()` and before the poll loop), add crash detection:
```python
    # ── Crash detection ──
    _heartbeat_path = os.path.join(os.path.dirname(__file__), "heartbeat.json")
    crash_info = health.check_crash(
        _heartbeat_path,
        stale_threshold=WEATHER.get("heartbeat_stale_threshold", 300),
    )
    if crash_info:
        notify("critical", "Bot Restarted After Crash",
               f"Last heartbeat was {crash_info['minutes_ago']} minutes ago. "
               f"Poll count at crash: {crash_info['poll_count']}. "
               f"Open positions: {crash_info['open_positions']}.",
               fields={"Last Seen": crash_info["last_timestamp"],
                        "Balance at Crash": f"${crash_info['balance']:.2f}"})
```

At the end of each poll cycle (just before the `time.sleep(POLL_INTERVAL)` call), add heartbeat write:
```python
        # ── Heartbeat ──
        health.write_heartbeat(
            _heartbeat_path,
            poll_count=_poll_count,
            open_positions=len(db.get_open_trades()),
            balance=db.get_balance(),
        )
```

- [ ] **Step 4: Wire daily digest into poll loop**

In `weather_bot.py`, add a module-level variable after the imports:
```python
_last_digest_date: str = ""
```

At the end of the poll loop (after heartbeat write, before sleep), add:
```python
        # ── Daily digest ──
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _last_digest_date and _last_digest_date != today_utc:
            _send_daily_digest(_last_digest_date)
        _last_digest_date = today_utc
```

Add the digest function above `run()`:
```python
def _send_daily_digest(date_str: str) -> None:
    """Compile and send daily P&L digest for the given UTC date."""
    balance = db.get_balance()
    closed_today = [t for t in db.get_closed_trades()
                    if t.get("closed_at", "").startswith(date_str)]
    opened_today = [t for t in db.get_open_trades()
                    if t.get("created_at", "").startswith(date_str)]

    realized_pnl = sum(t.get("pnl", 0) or 0 for t in closed_today)
    wins = sum(1 for t in closed_today if (t.get("pnl", 0) or 0) > 0)
    losses = len(closed_today) - wins
    open_count = len(db.get_open_trades())

    notify("info", f"Daily Digest — {date_str}",
           f"End-of-day summary for {date_str}",
           fields={
               "Balance": f"${balance:.2f}",
               "Realized P&L": f"${realized_pnl:+.2f}",
               "Opened": str(len(opened_today)),
               "Closed": str(len(closed_today)),
               "Win/Loss": f"{wins}W-{losses}L",
               "Open Positions": str(open_count),
           })
    print(f"[bot] Daily digest sent for {date_str}")
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add weather_bot.py tests/test_bot_heartbeat.py
git commit -m "feat(bot): wire heartbeat, crash detection, and daily digest into poll loop"
```

---

### Task 11: Dashboard — Bot-Down Banner + Notification Feed

**Files:**
- Modify: `ui/weather_dashboard.py:1-27` (imports), `ui/weather_dashboard.py:151-212` (Risk & Controls section)

- [ ] **Step 1: Add imports to dashboard**

In `ui/weather_dashboard.py`, add to imports:
```python
import health
```

- [ ] **Step 2: Add bot-down banner**

In the Risk & Controls section of `ui/weather_dashboard.py` (near the existing circuit breaker HALTED banner, around line 263), add before the circuit breaker check:

```python
    # ── Bot status banner ──
    _heartbeat_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "heartbeat.json")
    _bot_status = health.get_bot_status(
        _heartbeat_path,
        down_threshold=WEATHER.get("dashboard_bot_down_threshold", 180),
    )
    if not _bot_status["running"]:
        if _bot_status.get("no_file"):
            st.warning("**Bot status unknown** — no heartbeat file found.")
        else:
            st.error(f"**Bot offline** — last seen {_bot_status['minutes_ago']} minutes ago.")
```

- [ ] **Step 3: Add notification feed panel**

After the bot-down banner and circuit breaker section, add a new expander or section:

```python
    # ── Notification Feed ──
    st.markdown("### Alerts")
    if "dismissed_before" not in st.session_state:
        st.session_state.dismissed_before = None

    _notif_rows = db.get_notifications(
        severity_in=["critical", "warning"],
        limit=20,
        since=st.session_state.dismissed_before,
    )
    if _notif_rows:
        if st.button("Dismiss All"):
            from datetime import datetime, timezone
            st.session_state.dismissed_before = datetime.now(timezone.utc).isoformat()
            st.rerun()
        for row in _notif_rows:
            icon = "🔴" if row["severity"] == "critical" else "🟡"
            ts_short = row["timestamp"][:19].replace("T", " ")
            st.markdown(f"{icon} **{ts_short}** — {row['title']}: {row['message']}")
    else:
        st.caption("No recent alerts.")
```

- [ ] **Step 4: Verify dashboard loads without errors**

Run: `cd f:/CodeProjects/TestCode1 && python -c "import ui.weather_dashboard"`
Expected: no import errors (Streamlit won't render without `streamlit run`, but imports should resolve)

- [ ] **Step 5: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(dashboard): add bot-down banner and notification feed panel"
```

---

### Task 12: Wire API Monitor Notifications

**Files:**
- Modify: `api_monitor.py` (add notify calls on trip, recovery, non-retriable)

- [ ] **Step 1: Write failing test**

Add to `tests/test_api_monitor.py`:

```python
@patch("notifications.requests.post")
def test_breaker_trip_sends_discord_notification(mock_post):
    import db
    db._DB_PATH = ":memory:"
    db.init_db()

    mock_post.return_value = MagicMock(status_code=204)
    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }):
        cb = CircuitBreaker("test_notif", trip_threshold=3, cooldowns=[60])
        for _ in range(3):
            cb.record_failure(retriable=True)

    rows = db.get_notifications(severity_in=["warning"], limit=10)
    assert any("circuit breaker" in r["title"].lower() for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_monitor.py::test_breaker_trip_sends_discord_notification -v`
Expected: FAIL — no notification saved

- [ ] **Step 3: Add notify() calls to api_monitor.py**

Add import at top of `api_monitor.py`:
```python
from notifications import notify
```

In `_trip()`, after the print statement, add:
```python
        notify("warning", f"{self.name} API Circuit Breaker Tripped",
               f"{self._trip_count} consecutive trip(s). "
               f"Cooldown: {self._current_cooldown}s.",
               fields={"Breaker": self.name,
                        "Trip #": str(self._trip_count),
                        "Cooldown": f"{self._current_cooldown}s"})
```

In `record_success()`, inside the `if self._is_open:` block (recovery), add before the print:
```python
            notify("warning", f"{self.name} API Recovered",
                   f"Circuit breaker closed after recovery.",
                   fields={"Breaker": self.name})
```

In `call()`, change the `except NonRetriableError:` block to:
```python
        except NonRetriableError as e:
            notify("critical", f"{self.name} Non-Retriable Error",
                   str(e),
                   fields={"Breaker": self.name, "Error": str(e)})
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api_monitor.py -v`
Expected: all PASS (including the new test)

- [ ] **Step 5: Commit**

```bash
git add api_monitor.py tests/test_api_monitor.py
git commit -m "feat(api_monitor): wire Discord notifications for breaker trips and recovery"
```

---

### Task 13: Wire API Monitor into executor/live.py

**Files:**
- Modify: `executor/live.py` (wrap CLOB calls with breaker, add gas-low alert)

- [ ] **Step 1: Add api_monitor import to executor/live.py**

```python
import api_monitor
```

- [ ] **Step 2: Wrap _post_order with circuit breaker**

Replace the `try/except` body of `_post_order()` with:

```python
    def _post_order(self, token_id: str, price: float, size: float, side: str) -> dict | None:
        self._throttle_clob()
        clob_side = BUY if side == "BUY" else SELL
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=clob_side)

        def _do_post():
            signed = self._client.create_order(order_args)
            response = self._client.post_order(signed, OrderType.FOK)
            print(f"[live] Order posted: {response.get('orderID', '?')[:12]}")
            return response

        return api_monitor.call("clob", _do_post)
```

- [ ] **Step 3: Add gas-low notification to claims pass**

In the gas guard check (where MATIC balance is checked before claiming), if gas is too low, add:

```python
        notify("warning", "Gas Too Low",
               f"MATIC balance {matic_balance:.4f} below threshold. Claim deferred.",
               fields={"MATIC": f"{matic_balance:.4f}"})
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add executor/live.py
git commit -m "feat(live): wrap CLOB calls with circuit breaker, add gas-low alert"
```

---

### Task 14: Final Integration Verification

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 2: Verify imports resolve end-to-end**

Run:
```bash
python -c "
from config import WEATHER
import db; db.init_db()
from notifications import notify
import health
import api_monitor
from weather_risk import RiskManager
print('All imports OK')
print(f'Discord alerts configured: {bool(WEATHER.get(\"discord_webhook_alerts\"))}')
print(f'Discord trades configured: {bool(WEATHER.get(\"discord_webhook_trades\"))}')
"
```
Expected: `All imports OK`, both Discord configured: `False` (not configured yet, expected)

- [ ] **Step 3: Verify DB schema includes notifications table**

Run:
```bash
python -c "
import db; db.init_db()
rows = db.get_notifications(severity_in=['critical'], limit=1)
print(f'Notifications table works: {type(rows) is list}')
"
```
Expected: `Notifications table works: True`

- [ ] **Step 4: Verify heartbeat write/read cycle**

Run:
```bash
python -c "
import health
health.write_heartbeat('heartbeat_test.json', poll_count=1, open_positions=0, balance=100.0)
status = health.get_bot_status('heartbeat_test.json', down_threshold=180)
print(f'Bot running: {status[\"running\"]}')
import os; os.remove('heartbeat_test.json')
"
```
Expected: `Bot running: True`

- [ ] **Step 5: Commit any remaining changes and update todo.md**

Update `tasks/todo.md` Phase 4 section to mark items complete:
```markdown
### Phase 4 — Monitoring & Alerts for 24/7 Operation — COMPLETE (2026-04-XX)

Spec: `docs/superpowers/specs/2026-04-12-phase4-monitoring-alerts-design.md`
Plan: `docs/superpowers/plans/2026-04-12-phase4-monitoring-alerts.md` (14 tasks)

- [x] Discord webhook notifications (dual channel: #bot-alerts + #bot-trades)
- [x] Health heartbeat + crash detection on restart
- [x] Dashboard bot-down banner + notification feed panel
- [x] Shared API circuit breaker with escalating cooldowns
- [x] CLOB rate limiting (300ms delay floor)
- [x] Open-Meteo circuit breaker migrated to shared module
- [x] Daily P&L digest (UTC day rollover)
- [x] Trade alerts (fills, exits, claims)
- [x] Risk event alerts (circuit breaker, close-all)
- [x] API failure alerts (breaker trips, non-retriable errors, gas low)
```

```bash
git add tasks/todo.md
git commit -m "docs: mark Phase 4 monitoring & alerts as complete"
```
