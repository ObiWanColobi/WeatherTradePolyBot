"""Smoke test — cold-cache ensemble fallback in _write_phase2_snapshot.

Verifies:
  1. When the layer3 cache has no member temps for the target date, the
     snapshot writer falls back to a fresh get_ensemble_forecasts call.
  2. The fallback also seeds weather_ensemble_history so trajectory backfill
     starts immediately for that city (rather than waiting for scan TTL).
  3. save_ensemble_history's row count is correctly reported.
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


def main() -> int:
    target_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    fake_ensemble = [
        {"date": target_date, "member_temps": [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]},
        {"date": (datetime.fromisoformat(target_date).date() + timedelta(days=1)).isoformat(),
         "member_temps": [21.0, 22.0, 23.0, 24.0, 25.0, 26.0]},
    ]

    # 1. save_ensemble_history rowcount
    print("\n--- save_ensemble_history row count ---")
    n_first = db.save_ensemble_history("tokyo", fake_ensemble)
    n_second = db.save_ensemble_history("tokyo", fake_ensemble)  # all conflicts
    print(f"  first call:  inserted {n_first} rows (expected 2)")
    print(f"  second call: inserted {n_second} rows (expected 0)")
    assert n_first == 2, n_first
    assert n_second == 0, n_second

    # 2. Cold-cache fallback path: empty layer3 cache, stub get_ensemble_forecasts
    print("\n--- cold-cache fallback ---")
    import weather_decision as wd
    import layers.layer3_weather as layer3

    # Ensure layer3 cache is empty
    layer3._cache.clear()

    # Stub fresh ensemble fetch on the binding inside weather_decision
    fetch_calls = []
    def _stub_get_ensemble(lat, lon, tz="auto", days=4):
        fetch_calls.append((lat, lon, tz))
        return fake_ensemble
    wd.get_ensemble_forecasts = _stub_get_ensemble

    # Stub OM deterministic + Polymarket trades to keep test offline
    wd.get_deterministic_per_model = lambda *a, **kw: []
    import markets.polymarket as pm
    pm.get_market_trades = lambda *a, **kw: []

    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-cold",
        "market_name":     "Cold cache smoke",
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

    # Wipe sidecar from step 1 so we can verify the fallback re-seeds
    with db.get_conn() as conn:
        conn.execute("DELETE FROM weather_ensemble_history")

    wd._write_phase2_snapshot(sizing_id, "tokyo", target_date, "smoke-cond")

    print(f"  fresh-fetch calls made: {len(fetch_calls)} (expected 1)")
    assert len(fetch_calls) == 1, fetch_calls

    with db.get_conn() as conn:
        ds = conn.execute(
            "SELECT ens_std, ens_p05, ens_p95 FROM decision_snapshots "
            "WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()
        seeded = conn.execute(
            "SELECT COUNT(*) AS n FROM weather_ensemble_history WHERE city = 'tokyo'"
        ).fetchone()

    print(f"  ens_std={ds['ens_std']}, ens_p05={ds['ens_p05']}, ens_p95={ds['ens_p95']}")
    print(f"  sidecar rows seeded: {seeded['n']} (expected 2)")
    assert ds["ens_std"] is not None and ds["ens_std"] > 0.0, ds["ens_std"]
    assert seeded["n"] == 2, seeded["n"]

    # 3. Hot cache: should NOT trigger the fallback
    print("\n--- hot-cache path skips fallback ---")
    import time as _time
    layer3._cache["tokyo"] = {"ts": _time.time(), "forecast": [], "ensemble": fake_ensemble}
    fetch_calls.clear()

    sid2 = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-hot",
        "market_name":     "Hot cache smoke",
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
    wd._write_phase2_snapshot(sid2, "tokyo", target_date, "smoke-cond-hot")
    print(f"  fresh-fetch calls made: {len(fetch_calls)} (expected 0)")
    assert len(fetch_calls) == 0, fetch_calls

    print("\n  PASS — fallback fires on cold cache, skipped on hot cache, sidecar seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
