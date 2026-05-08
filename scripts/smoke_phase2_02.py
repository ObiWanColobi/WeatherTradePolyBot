"""Phase2-02 local smoke test — ensemble spread capture.

Two checks:
  1. _ensemble_spread_stats produces sensible numbers on synthetic input.
  2. End-to-end: prime the layer3 forecast cache for a real city, run
     _write_phase2_snapshot, confirm decision_snapshots row has populated
     ens_std / ens_iqr / ens_p05 / ens_p95.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
print(f"Using temp DB: {_tmp.name}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
config.DB_PATH = _tmp.name

import db  # noqa: E402
db._DB_PATH = _tmp.name
db.init_db()

from weather_decision import _ensemble_spread_stats, _write_phase2_snapshot  # noqa: E402
from layers.layer3_weather import CITY_COORDS, _cache as _layer3_cache  # noqa: E402
from markets.open_meteo import get_ensemble_forecasts  # noqa: E402


def _check_synthetic() -> bool:
    print("\n--- Synthetic spread check ---")
    members = [20.0, 21.0, 22.0, 23.0, 24.0]
    out = _ensemble_spread_stats(members)
    print(f"  members={members}")
    print(f"  out={out}")
    assert out["ens_p05"] is not None and out["ens_p95"] is not None
    assert out["ens_p05"] < out["ens_p95"]
    assert out["ens_std"] > 0.0
    assert out["ens_iqr"] > 0.0

    # Empty / too-few members -> all NULL
    assert _ensemble_spread_stats(None) == {"ens_std": None, "ens_iqr": None, "ens_p05": None, "ens_p95": None}
    assert _ensemble_spread_stats([22.0]) == {"ens_std": None, "ens_iqr": None, "ens_p05": None, "ens_p95": None}
    print("  PASS")
    return True


def _check_end_to_end() -> int:
    print("\n--- End-to-end with real OM fetch ---")
    city = "tokyo"
    coords = CITY_COORDS[city]
    target_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    # Prime layer3 cache (mimicking what forecast_cache_for_city would do)
    print(f"  fetching ensemble for {city}...")
    ensemble = get_ensemble_forecasts(coords["lat"], coords["lon"], coords["tz"])
    if not ensemble:
        print("  FAIL: ensemble fetch returned empty")
        return 1
    member_count = len(ensemble[0].get("member_temps") or [])
    print(f"  ensemble fetched: {len(ensemble)} days, {member_count} members on day 0")

    import time
    _layer3_cache[city] = {"ts": time.time(), "forecast": [], "ensemble": ensemble}

    # Fake sizing_decisions row
    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-test-phase2-02",
        "market_name":     "Smoke test phase2-02",
        "city":            city,
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

    _write_phase2_snapshot(sizing_id, city, target_date)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT det_icon_temp_c, det_gfs_temp_c, ens_std, ens_iqr, "
            "ens_p05, ens_p95 FROM decision_snapshots "
            "WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()

    print("\n  Row written:")
    for k in row.keys():
        v = row[k]
        if v is not None and isinstance(v, float):
            print(f"    {k:<20} = {v:.3f}")
        else:
            print(f"    {k:<20} = {v}")

    bad = [k for k in ("ens_std", "ens_iqr", "ens_p05", "ens_p95") if row[k] is None]
    if bad:
        print(f"\n  FAIL: spread cols NULL: {bad}")
        return 1
    if not (row["ens_p05"] < row["ens_p95"]):
        print(f"\n  FAIL: p05 ({row['ens_p05']}) >= p95 ({row['ens_p95']})")
        return 1
    print("\n  PASS — spread columns populated and self-consistent")
    return 0


if __name__ == "__main__":
    _check_synthetic()
    sys.exit(_check_end_to_end())
