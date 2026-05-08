"""Phase2-05 local smoke test — Polymarket trade-velocity capture.

Stubs get_market_trades to return synthetic trades, then exercises:
  1. get_trade_velocity computes per-minute rates correctly
  2. TTL cache short-circuits the second call within the window
  3. _write_phase2_snapshot lands trades_per_min_30 / 60 columns
"""

from __future__ import annotations

import os
import sys
import tempfile
import time as _time
from datetime import datetime, timezone
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

import markets.polymarket as pm  # noqa: E402


def _stub_trades(condition_id: str, limit: int = 500):
    """20 trades evenly spaced over last 60min, offsets 1.5..58.5 min so none
    sits on a window boundary. Result: n_30 == 10, n_60 == 20.
    """
    now = _time.time()
    trades = []
    for i in range(20):
        ts = now - ((i * 3 + 1.5) * 60)
        trades.append({"timestamp": ts, "price": 0.5, "size": 10.0,
                       "side": "BUY", "outcome": "YES"})
    return trades


def _check_velocity_unit() -> bool:
    print("\n--- velocity unit checks ---")
    pm._VELOCITY_CACHE.clear()
    pm.get_market_trades = _stub_trades

    v = pm.get_trade_velocity("M_v1")
    print(f"  v={v}")
    # 30-min window: trades at t-0, -3, -6, ..., -27 -> 10 trades. n_60=20.
    assert v["n_60"] == 20, v
    assert abs(v["trades_per_min_30"] - (10 / 30)) < 1e-9, v
    assert abs(v["trades_per_min_60"] - (20 / 60)) < 1e-9, v

    # TTL cache: swap out get_market_trades; cached call should return same
    pm.get_market_trades = lambda *a, **kw: []
    v2 = pm.get_trade_velocity("M_v1")
    assert v2["n_60"] == 20, "expected cache hit"
    print(f"  cache-hit:  {v2}  OK")

    # Different market_id forces a fresh fetch (now zeros)
    v3 = pm.get_trade_velocity("M_v2")
    assert v3["n_60"] == 0, v3
    print(f"  cache-miss: {v3}  OK")

    print("  PASS")
    return True


def _check_end_to_end() -> int:
    print("\n--- end-to-end via _write_phase2_snapshot ---")
    pm._VELOCITY_CACHE.clear()
    pm.get_market_trades = _stub_trades

    from weather_decision import _write_phase2_snapshot
    # Stub the OM deterministic call to avoid network noise
    import markets.open_meteo as om
    om.get_deterministic_per_model = lambda *a, **kw: []

    today = datetime.now(timezone.utc).date().isoformat()
    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-test-vel",
        "market_name":     "Smoke vel",
        "city":            "tokyo",
        "direction":       "no",
        "balance":         3900.0,
        "model_prob":      0.7,
        "market_price":    0.55,
        "p_used":          0.7,
        "price_used":      0.55,
        "ensemble_n":      71,
        "days_to_resolution": 0,
        "ensemble_margin_c":  2.5,
        "is_unanimous":    0,
        "edge":            0.15,
        "odds":            0.82,
        "kelly_raw":       0.04,
        "ensemble_scale":  1.0,
        "horizon_mult":    1.0,
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

    _write_phase2_snapshot(sizing_id, "tokyo", today, "smoke-cond-id")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT trades_per_min_30, trades_per_min_60 "
            "FROM decision_snapshots WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()
    print(f"  row: tpm_30={row['trades_per_min_30']} tpm_60={row['trades_per_min_60']}")
    assert abs(row["trades_per_min_30"] - (10 / 30)) < 1e-9
    assert abs(row["trades_per_min_60"] - (20 / 60)) < 1e-9
    print("  PASS")
    return 0


if __name__ == "__main__":
    _check_velocity_unit()
    sys.exit(_check_end_to_end())
