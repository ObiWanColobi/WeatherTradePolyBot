import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
from bakeoff.harness.decision import decision_rows

def test_decision_rows_picks_one_row_per_bucket_at_a_single_time():
    df = pd.DataFrame([
        # two times, two buckets each
        {"snapshot_at_utc": "2026-06-01 00:00:00", "sub_market_condition_id": "A",
         "bound_lo_f": 70.0, "bound_hi_f": 72.0, "is_open_tail": 0, "best_ask": 0.3, "best_bid": 0.28, "group_item_title": "70-72"},
        {"snapshot_at_utc": "2026-06-01 00:00:00", "sub_market_condition_id": "B",
         "bound_lo_f": 72.0, "bound_hi_f": 74.0, "is_open_tail": 0, "best_ask": 0.2, "best_bid": 0.18, "group_item_title": "72-74"},
        {"snapshot_at_utc": "2026-06-01 11:00:00", "sub_market_condition_id": "A",
         "bound_lo_f": 70.0, "bound_hi_f": 72.0, "is_open_tail": 0, "best_ask": 0.5, "best_bid": 0.48, "group_item_title": "70-72"},
    ])
    ts, rows = decision_rows(df, hours_before_close=12)
    # one row per distinct bucket at the chosen time
    assert len({r["sub_market_condition_id"] for r in rows}) == len(rows)
    assert ts != ""
