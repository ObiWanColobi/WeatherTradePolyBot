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
