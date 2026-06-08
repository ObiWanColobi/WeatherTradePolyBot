from datetime import datetime, timezone, timedelta
from shotgun.fire_window import hours_to_close, in_fire_window


def test_hours_to_close_city_local_anchor():
    # Toronto res-date 2026-06-10 close = 2026-06-10 23:59:59 America/Toronto = 2026-06-11 03:59:59 UTC
    now = datetime(2026, 6, 11, 3, 59, 59, tzinfo=timezone.utc) - timedelta(hours=12)
    h = hours_to_close("toronto", "2026-06-10", now=now)
    assert 11.9 < h < 12.1


def test_in_fire_window_true_at_12h():
    now = datetime(2026, 6, 11, 3, 59, 59, tzinfo=timezone.utc) - timedelta(hours=12)
    assert in_fire_window("toronto", "2026-06-10", fire_window_hours=12.0, now=now) is True


def test_in_fire_window_false_at_24h():
    now = datetime(2026, 6, 11, 3, 59, 59, tzinfo=timezone.utc) - timedelta(hours=24)
    assert in_fire_window("toronto", "2026-06-10", fire_window_hours=12.0, now=now) is False


def test_in_fire_window_false_after_close():
    now = datetime(2026, 6, 11, 5, 0, 0, tzinfo=timezone.utc)   # past close
    assert in_fire_window("toronto", "2026-06-10", fire_window_hours=12.0, now=now) is False


def test_unknown_city_returns_none_and_false():
    assert hours_to_close("atlantis", "2026-06-10") is None
    assert in_fire_window("atlantis", "2026-06-10", fire_window_hours=12.0) is False
