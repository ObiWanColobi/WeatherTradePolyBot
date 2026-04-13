import db
from unittest.mock import patch, MagicMock


def setup_function():
    db._DB_PATH = ":memory:"
    db._MEM_CONN = None
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
