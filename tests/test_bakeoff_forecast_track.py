# tests/test_bakeoff_forecast_track.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
from bakeoff.run_forecast_track import build_records
from bakeoff.harness.loader import CityDay

def test_build_records_emits_members_and_truth_for_gradeable_days():
    snaps = pd.DataFrame([
        {"snapshot_at_utc": "2026-06-01 00:00:00", "sub_market_condition_id": "A",
         "bound_lo_f": 70.0, "bound_hi_f": 72.0, "is_open_tail": 0, "best_ask": 0.3,
         "best_bid": 0.28, "group_item_title": "70-72"},
    ])
    cds = [CityDay("nyc", "2026-06-01", "highest", snaps, 71.0),
           CityDay("dal", "2026-06-02", "highest", snaps, None)]  # no truth -> excluded
    gefs = pd.DataFrame([
        {"city": "nyc", "local_date": "2026-06-01", "init_ts": "2026-05-31 00:00:00", "daily_max_f": 71.0},
    ])
    recs = build_records(cds, gefs)
    assert len(recs) == 1                      # only the gradeable nyc day
    assert recs[0]["truth_f"] == 71.0
    assert recs[0]["member_temps"] == [71.0]
