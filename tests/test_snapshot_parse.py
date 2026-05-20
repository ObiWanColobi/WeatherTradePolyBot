import pytest
from snapshot_parse import (
    parse_event_slug,
    parse_bucket_bounds,
    classify_bucket_type,
    c_to_f,
)


def test_parse_event_slug_us_city():
    out = parse_event_slug("highest-temperature-in-nyc-on-may-15-2026")
    assert out == {"kind": "highest", "city": "nyc", "resolution_date": "2026-05-15"}


def test_parse_event_slug_multi_word_city():
    out = parse_event_slug("lowest-temperature-in-hong-kong-on-june-1-2026")
    assert out == {"kind": "lowest", "city": "hong-kong", "resolution_date": "2026-06-01"}


def test_parse_event_slug_invalid_returns_none():
    assert parse_event_slug("not-a-weather-event") is None
    assert parse_event_slug("") is None


def test_parse_bucket_bounds_closed_range_fahrenheit():
    assert parse_bucket_bounds("64-65°F") == (64.0, 65.0)
    assert parse_bucket_bounds("88-89°F") == (88.0, 89.0)


def test_parse_bucket_bounds_open_top_tail_fahrenheit():
    assert parse_bucket_bounds("74°F or higher") == (74.0, None)
    assert parse_bucket_bounds("104°F or above") == (104.0, None)


def test_parse_bucket_bounds_open_bottom_tail_fahrenheit():
    assert parse_bucket_bounds("55°F or below") == (None, 55.0)


def test_parse_bucket_bounds_exact_celsius_converts():
    # "be 22°C on May 21" — exact temperature in Celsius
    lo, hi = parse_bucket_bounds("be 22°C")
    # 22°C = 71.6°F → rounded for resolution
    assert lo == hi
    assert abs(lo - 71.6) < 0.01


def test_parse_bucket_bounds_celsius_range_converts():
    # "between 22-23°C"
    lo, hi = parse_bucket_bounds("22-23°C")
    assert abs(lo - 71.6) < 0.01
    assert abs(hi - 73.4) < 0.01


def test_parse_bucket_bounds_celsius_open_tail_converts():
    lo, hi = parse_bucket_bounds("22°C or higher")
    assert hi is None
    assert abs(lo - 71.6) < 0.01


def test_parse_bucket_bounds_garbage_returns_none():
    assert parse_bucket_bounds("nonsense") == (None, None)
    assert parse_bucket_bounds("") == (None, None)


def test_classify_bucket_type():
    assert classify_bucket_type("55°F or below") == "tail"
    assert classify_bucket_type("74°F or higher") == "tail"
    assert classify_bucket_type("64-65°F") == "range"
    assert classify_bucket_type("be 65°F") == "exact"
    assert classify_bucket_type("65°F") == "exact"


def test_c_to_f():
    assert c_to_f(0) == 32
    assert c_to_f(100) == 212
    assert abs(c_to_f(22) - 71.6) < 0.01
