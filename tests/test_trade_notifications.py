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
    assert any("Position Closed" in r["title"] for r in rows)
