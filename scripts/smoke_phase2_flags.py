"""E15.3-01 + E6-02 smoke test — derived trajectory + direction-agreement flags.

Drives _traj_regime_flag and _direction_agreement directly with synthetic
input, then exercises the full _write_phase2_snapshot path (with seeded
trajectory history) and verifies the flags land on sizing_decisions.
"""

from __future__ import annotations

import json
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

from weather_decision import _traj_regime_flag, _direction_agreement  # noqa: E402


def _check_traj_regime() -> bool:
    print("\n--- _traj_regime_flag ---")

    # Drift +1.5°C, monotone up, max-step 0.5°C  -> 1
    traj = {f"traj_init_d{n}": v for n, v in zip((5, 4, 3, 2, 1), [22.0, 22.5, 23.0, 23.0, 23.5])}
    assert _traj_regime_flag(traj) == 1, traj

    # Drift +0.4°C  -> 0 (below 1.0°C threshold)
    traj = {f"traj_init_d{n}": v for n, v in zip((5, 4, 3, 2, 1), [22.0, 22.1, 22.2, 22.3, 22.4])}
    assert _traj_regime_flag(traj) == 0, traj

    # Drift +1.5°C but non-monotone -> 0
    traj = {f"traj_init_d{n}": v for n, v in zip((5, 4, 3, 2, 1), [22.0, 23.0, 22.5, 23.0, 23.5])}
    assert _traj_regime_flag(traj) == 0, traj

    # Drift +1.5°C, monotone, but max-step 2.5°C -> 0
    traj = {f"traj_init_d{n}": v for n, v in zip((5, 4, 3, 2, 1), [22.0, 22.0, 22.0, 22.0, 24.5])}
    # max step is 2.5 (D-2 -> D-1) which violates < 2.0
    assert _traj_regime_flag(traj) == 0, traj

    # Missing D-3 -> None
    traj = {"traj_init_d5": 22.0, "traj_init_d4": 22.5, "traj_init_d3": None,
            "traj_init_d2": 23.0, "traj_init_d1": 23.5}
    assert _traj_regime_flag(traj) is None, traj

    # Monotone down -1.5°C with safe step -> 1
    traj = {f"traj_init_d{n}": v for n, v in zip((5, 4, 3, 2, 1), [25.0, 24.5, 24.0, 24.0, 23.5])}
    assert _traj_regime_flag(traj) == 1, traj

    print("  PASS")
    return True


def _check_direction_agreement() -> bool:
    print("\n--- _direction_agreement ---")

    # Tokyo (default ICON), threshold >=27C, ICON 28°C (det says YES),
    # calibrated 0.7 (model says YES) -> agree
    flag, best_t, best_m = _direction_agreement("tokyo", 28.0, 25.0, 27.0, ">=", 0.7)
    assert (flag, best_t, best_m) == (1, 28.0, "icon"), (flag, best_t, best_m)

    # Disagree: ICON says NO (24°C below 27°C), calibrated 0.7 (YES)
    flag, _, _ = _direction_agreement("tokyo", 24.0, 28.0, 27.0, ">=", 0.7)
    assert flag == 0, flag

    # Paris uses GFS as best (per E3 mapping)
    flag, best_t, best_m = _direction_agreement("paris", 24.0, 22.0, 23.0, ">=", 0.6)
    # GFS best 22.0 < 23.0 -> det NO; calibrated 0.6 -> YES -> disagree
    assert (flag, best_t, best_m) == (0, 22.0, "gfs"), (flag, best_t, best_m)

    # Missing input -> None
    flag, _, _ = _direction_agreement("tokyo", None, 25.0, 27.0, ">=", 0.7)
    assert flag is None, flag

    # Below-threshold market (<= operator)
    flag, _, _ = _direction_agreement("tokyo", 26.0, 25.0, 27.0, "<=", 0.6)
    # ICON 26 <= 27 -> det YES; calibrated 0.6 -> YES -> agree
    assert flag == 1, flag

    print("  PASS")
    return True


def _check_end_to_end() -> int:
    print("\n--- end-to-end via _write_phase2_snapshot ---")

    target_date = "2026-05-13"
    target_dt   = datetime.fromisoformat(target_date).date()

    # Seed monotone-up trajectory drift +1.5°C
    inits = {5: 22.0, 4: 22.5, 3: 23.0, 2: 23.0, 1: 23.5}
    for n, mid in inits.items():
        init = (target_dt - timedelta(days=n)).isoformat()
        members = [mid - 0.2, mid, mid + 0.2]
        with db.get_conn() as conn:
            conn.execute("""
                INSERT INTO weather_ensemble_history
                    (city, init_date, target_date, member_temps, captured_at)
                VALUES (?, ?, ?, ?, ?)
            """, ("tokyo", init, target_date, json.dumps(members), init + "T12:00:00+00:00"))

    # Stub OM: ICON 28°C (says YES), GFS 25°C (says NO) — det best for tokyo is ICON
    # so direction = ICON YES vs calibrated 0.7 YES -> agree=1.
    # `from markets.open_meteo import get_deterministic_per_model` in weather_decision
    # creates a separate binding, so we have to patch the imported name directly.
    import weather_decision as wd
    import markets.polymarket as pm
    pm.get_market_trades = lambda *a, **kw: []

    def _stub_det(lat, lon, model, **kw):
        if "icon" in model:
            return [{"date": target_date, "temp_max_c": 28.0}]
        return [{"date": target_date, "temp_max_c": 25.0}]
    wd.get_deterministic_per_model = _stub_det

    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-flags",
        "market_name":     "Smoke flags",
        "city":            "tokyo",
        "direction":       "yes",
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

    from weather_decision import _write_phase2_snapshot
    _write_phase2_snapshot(
        sizing_id, "tokyo", target_date, "smoke-cond",
        target_str=">=27C",
        calibrated_prob=0.7,
    )

    with db.get_conn() as conn:
        sd = conn.execute(
            "SELECT traj_regime_flag, direction_agreement_flag FROM sizing_decisions WHERE id = ?",
            (sizing_id,),
        ).fetchone()
        ds = conn.execute(
            "SELECT det_best_temp_c, det_best_model FROM decision_snapshots WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()

    print(f"  sizing_decisions: traj_regime_flag={sd['traj_regime_flag']} "
          f"direction_agreement_flag={sd['direction_agreement_flag']}")
    print(f"  decision_snapshots: det_best_temp_c={ds['det_best_temp_c']} "
          f"det_best_model={ds['det_best_model']!r}")

    assert sd["traj_regime_flag"] == 1, sd["traj_regime_flag"]
    assert sd["direction_agreement_flag"] == 1, sd["direction_agreement_flag"]
    assert abs(ds["det_best_temp_c"] - 28.0) < 1e-9
    assert ds["det_best_model"] == "icon"
    print("\n  PASS — both flags fire and det best is logged")
    return 0


if __name__ == "__main__":
    _check_traj_regime()
    _check_direction_agreement()
    sys.exit(_check_end_to_end())
