import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
import pytest
from bakeoff.forecast.loader import canon_city, members_asof

def test_canon_city_normalizes_slug():
    assert canon_city("buenos-aires") == "buenos aires"
    assert canon_city("NYC") == "nyc"

def test_members_asof_picks_latest_init_at_or_before_decision():
    # two inits for the same city-day; decision time excludes the later init
    df = pd.DataFrame([
        {"city": "nyc", "local_date": "2026-06-01", "init_ts": "2026-05-31 00:00:00", "daily_max_f": 70.0},
        {"city": "nyc", "local_date": "2026-06-01", "init_ts": "2026-05-31 00:00:00", "daily_max_f": 71.0},
        {"city": "nyc", "local_date": "2026-06-01", "init_ts": "2026-06-01 12:00:00", "daily_max_f": 99.0},
    ])
    # decision at 2026-06-01 06:00 -> only the 05-31 init is usable (PIT)
    out = members_asof(df, "nyc", "2026-06-01", "2026-06-01 06:00:00")
    assert sorted(out) == [70.0, 71.0]   # the 99.0 future-init member is excluded


def test_members_asof_real_dtypes_no_lexical_lookahead():
    # Real DB dtypes: datetime64 columns + a non-midnight init + a TZ-aware decision_ts.
    # A lexical string compare would let the 03:00 future init leak in (space < 'T'),
    # producing lookahead. With real timestamp comparison it must be excluded.
    df = pd.DataFrame({
        "city": ["nyc", "nyc", "nyc"],
        "local_date": pd.to_datetime(["2026-03-31", "2026-03-31", "2026-03-31"]),
        "init_ts": pd.to_datetime(["2026-03-30 00:00:00",
                                   "2026-03-30 00:00:00",
                                   "2026-03-31 03:00:00"]),  # future-of-decision init
        "daily_max_f": [50.0, 51.0, 99.0],
    })
    # decision 2026-03-31 00:00:00Z (tz-aware) -> the 03:00 init is AFTER -> excluded
    out = members_asof(df, "nyc", "2026-03-31", "2026-03-31T00:00:00+00:00")
    assert sorted(out) == [50.0, 51.0]   # 99.0 (future init) must NOT leak in
