"""Fire-window gate: a city-day fires once it is within fire_window_hours of
close. Close = resolution_date 23:59:59 in city-local tz (when daily-max locks).
Do NOT use event end timestamps from the market API (markets trade ~12h past)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shotgun.city_tz import CITY_TZ


def _canon(city: str) -> str:
    return city.lower().replace("-", " ")


def _close_utc(city: str, resolution_date: str) -> datetime | None:
    tz = CITY_TZ.get(_canon(city))
    if tz is None:
        return None
    local = datetime.fromisoformat(resolution_date + "T23:59:59").replace(tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc)


def hours_to_close(city: str, resolution_date: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    close = _close_utc(city, resolution_date)
    if close is None:
        return None
    return (close - now).total_seconds() / 3600.0


def in_fire_window(city: str, resolution_date: str, fire_window_hours: float,
                   now: datetime | None = None) -> bool:
    """True iff 0 < hours_to_close <= fire_window_hours (inside window, before close)."""
    h = hours_to_close(city, resolution_date, now=now)
    if h is None:
        return False
    return 0.0 < h <= fire_window_hours
