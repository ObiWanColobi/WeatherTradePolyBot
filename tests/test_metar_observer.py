"""Tests for markets.metar_observer state management."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from markets.metar_observer import (
    CITY_RESOLVERS,
    MetarReading,
    _STATE,
    _update_city_state,
)


def _reading(observed_at_utc: datetime, temp_c: float, icao: str = "LLBG") -> MetarReading:
    return MetarReading(
        icao=icao,
        observed_at_utc=observed_at_utc,
        temp_c=temp_c,
        dewpoint_c=None,
        raw_report="",
        source="avwx",
    )


def test_max_today_resets_on_local_day_rollover():
    """Bug A regression — Tel Aviv #61 (2026-05-12).

    On May 12 the TLV bot accumulated max_today_c=34°C from a 16:00 TLV reading.
    At 21:00 UTC the local clock crossed into May 13 (TLV is UTC+3), but the
    accumulator kept the 34°C from yesterday, causing a false "observed lock YES"
    on a market that ultimately resolved NO at 24.4°C.

    After the fix, the prior day's max must be wiped the first time we evaluate
    on a new local date.
    """
    city = "tel aviv"
    meta = CITY_RESOLVERS[city]
    tz   = meta["tz"]
    icao = meta["icao"]

    # Isolate from other tests
    _STATE.pop(city, None)

    # ── Day 1: afternoon reading sets max_today=34°C ────────────────────────
    # May 12 13:00 UTC = 16:00 TLV (UTC+3) — a real local afternoon.
    day1_now = datetime(2026, 5, 12, 13, 0, tzinfo=timezone.utc)
    # The plausibility gate rejects the very first reading as "no-prior-reading"
    # and buffers it; the second reading within 2°C confirms both. So we feed
    # two close readings to escape bootstrap and populate max_today.
    with patch("markets.metar_observer._RECORD_TO_DB", False):
        _update_city_state(
            city, icao, tz,
            new_readings=[_reading(day1_now - timedelta(minutes=20), 33.5),
                          _reading(day1_now, 34.0)],
            now_utc=day1_now,
        )

    state = _STATE[city]
    assert state.max_today_c == 34.0
    assert state.max_today_local_date == date(2026, 5, 12)

    # ── Day 2: local clock rolls into May 13 TLV ────────────────────────────
    # May 12 21:00 UTC = 00:00 TLV May 13 — the local date has advanced.
    # We feed a 30°C reading (within the 5°C plausibility delta of the prior
    # 34°C reading so the gate accepts it) timestamped at the May 12/13 TLV
    # boundary. Its local date is May 13.
    day2_now = datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc)
    with patch("markets.metar_observer._RECORD_TO_DB", False):
        _update_city_state(
            city, icao, tz,
            new_readings=[_reading(day2_now, 30.0)],
            now_utc=day2_now,
        )

    state = _STATE[city]
    # Pre-fix: max_today would still be 34.0 (carried forward from May 12)
    # because 30°C < 34°C so the comparison never replaced it.
    # Post-fix: rollover reset wiped max to None, then the May 13 reading
    # populates it fresh at 30°C.
    assert state.max_today_c == 30.0, (
        "rollover reset must wipe yesterday's max before new-day readings apply"
    )
    assert state.max_today_local_date == date(2026, 5, 13)
