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
