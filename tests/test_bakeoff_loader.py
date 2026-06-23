import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.loader import CityDay, time_split

def _cd(date, city="nyc"):
    return CityDay(city=city, resolution_date=date, kind="highest", snapshots=None, truth_f=None)

def test_time_split_holds_out_latest_dates():
    days = [_cd(f"2026-06-{d:02d}") for d in range(1, 31)]  # 30 distinct dates
    tune, holdout = time_split(days, holdout_frac=0.34)
    tune_dates = {d.resolution_date for d in tune}
    holdout_dates = {d.resolution_date for d in holdout}
    # No date appears in both
    assert tune_dates.isdisjoint(holdout_dates)
    # Holdout is the LATEST ~34% of dates
    assert max(tune_dates) < min(holdout_dates)
    assert 9 <= len(holdout_dates) <= 11  # ~34% of 30
