import db


def setup_function():
    """Reset DB to in-memory for each test."""
    db._DB_PATH = ":memory:"
    db._MEM_CONN = None  # force a fresh in-memory connection each test
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
