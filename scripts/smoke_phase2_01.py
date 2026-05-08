"""Phase2-01 local smoke test.

Drives the Phase 2 snapshot writer end-to-end:
  - hits the live Open-Meteo forecast endpoint for ICON + GFS seamless
  - inserts a fake sizing_decisions row
  - calls _write_phase2_snapshot
  - reads decision_snapshots back and prints the row

Run from project root:
    python scripts/smoke_phase2_01.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force a temporary DB so we don't touch the real one
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
print(f"Using temp DB: {_tmp.name}")

# Also reload config so DB_PATH change is picked up. We import config first
# to seed the path, then import db and the decision module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
config.DB_PATH = _tmp.name

import db  # noqa: E402
db._DB_PATH = _tmp.name
db.init_db()

from weather_decision import _write_phase2_snapshot  # noqa: E402


def main() -> int:
    today = datetime.now(timezone.utc).date()
    target = (today + timedelta(days=1)).isoformat()  # tomorrow

    # Insert a placeholder sizing_decisions row to get an id we can FK on
    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-test-market",
        "market_name":     "Smoke test — will Tokyo be >25C tomorrow?",
        "city":            "tokyo",
        "direction":       "no",
        "balance":         3900.0,
        "model_prob":      0.7,
        "market_price":    0.55,
        "p_used":          0.7,
        "price_used":      0.55,
        "ensemble_n":      71,
        "days_to_resolution": 1,
        "ensemble_margin_c":  2.5,
        "is_unanimous":    0,
        "edge":            0.15,
        "odds":            0.82,
        "kelly_raw":       0.04,
        "ensemble_scale":  1.0,
        "horizon_mult":    0.85,
        "margin_mult":     1.0,
        "kelly_fraction":  0.5,
        "kelly_final":     0.02,
        "size_pre_cap":    78.0,
        "cap_per_bet":     50.0,
        "cap_balance_pct": 1170.0,
        "size_after_caps": 50.0,
        "final_size":      50.0,
        "binding_constraint": "cap_per_bet",
        "raw_prob":        0.7,
        "calibrated_prob": 0.7,
        "fixed_mode_stake_usdc": 50.0,
    })
    print(f"sizing_id = {sizing_id}")

    print(f"calling _write_phase2_snapshot(city='tokyo', res_date='{target}')...")
    _write_phase2_snapshot(sizing_id, "tokyo", target)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, sizing_decision_id, captured_at, "
            "det_icon_temp_c, det_gfs_temp_c, det_source_tag "
            "FROM decision_snapshots WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()

    if row is None:
        print("FAIL: no decision_snapshots row written")
        return 1

    print("\nRow written:")
    for k in row.keys():
        print(f"  {k:<22} = {row[k]}")

    if row["det_icon_temp_c"] is None or row["det_gfs_temp_c"] is None:
        print("\nWARNING: one or both deterministic temps are NULL.")
        print("(could be transient OM 429 / network — re-run if needed.)")
        return 2

    print("\nPASS — both deterministic temps populated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
