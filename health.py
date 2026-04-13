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
