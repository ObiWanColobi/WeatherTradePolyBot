"""Tests that risk events send Discord notifications via notify()."""

import importlib
import db
from unittest.mock import patch, MagicMock


def setup_function():
    db._DB_PATH = ":memory:"
    db._MEM_CONN = None
    db.init_db()


@patch("notifications.requests.post")
def test_circuit_breaker_trip_sends_notification(mock_post):
    mock_post.return_value = MagicMock(status_code=204)

    with patch("notifications.WEATHER", {
        "discord_webhook_alerts": "https://discord.com/api/webhooks/test",
        "discord_webhook_trades": "",
    }):
        # Reload weather_risk to pick up the patched WEATHER in notifications
        import weather_risk
        importlib.reload(weather_risk)
        from weather_risk import RiskManager, RiskState

        rm = RiskManager()

        # Mock db calls so circuit breaker sees 15% loss (at the limit)
        # Account: $100 balance, no open trades => account_value = $100
        # Losses: $15 => loss_pct = 0.15 which equals limit (0.15) => trips
        with patch("weather_risk.db.get_today_realized_losses", return_value=(1, 15.0)), \
             patch("weather_risk.db.get_balance", return_value=100.0), \
             patch("weather_risk.db.get_open_trades", return_value=[]), \
             patch("weather_risk.WEATHER", {
                 "risk_auto_reset": False,
                 "risk_daily_loss_limit_pct": 0.15,
                 "risk_email_enabled": False,
             }):
            state = rm.check()

        assert state == RiskState.HALTED, "Expected circuit breaker to trip"

    # After triggering, verify notification was persisted to DB
    rows = db.get_notifications(severity_in=["critical"], limit=10)
    assert any(
        "circuit breaker" in r["title"].lower() or "circuit breaker" in r["message"].lower()
        for r in rows
    ), f"Expected a 'circuit breaker' critical notification in DB, got: {rows}"
