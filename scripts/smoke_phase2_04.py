"""Phase2-04 local smoke test — multi-init GEFS-31 trajectory.

Seeds weather_ensemble_history with synthetic ensembles for D-5..D-1 inits
toward a target date, then drives _write_phase2_snapshot and verifies the
ten trajectory columns populate. Also covers the missing-init NULL path.

Zero network calls — sidecar table is pure DB.
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


def _seed_init(city: str, init_date: str, target_date: str, members: list[float]):
    captured_iso = init_date + "T12:00:00+00:00"
    with db.get_conn() as conn:
        conn.execute("""
            INSERT INTO weather_ensemble_history
                (city, init_date, target_date, member_temps, captured_at)
            VALUES (?, ?, ?, ?, ?)
        """, (city, init_date, target_date, json.dumps(members), captured_iso))


def main() -> int:
    target_date = "2026-05-13"
    target_dt   = datetime.fromisoformat(target_date).date()

    # Seed D-5, D-4, D-3, D-1; intentionally skip D-2 to test NULL coverage.
    print("\n--- seeding ---")
    seeds = {
        5: [22.0, 23.0, 24.0, 25.0, 26.0],          # D-5: spread 1.41, mean 24.0
        4: [22.5, 23.5, 24.0, 24.5, 25.5],          # D-4: tighter, mean 24.0
        3: [23.0, 23.5, 24.0, 24.5, 25.0],          # D-3: tighter still
        1: [23.8, 24.0, 24.0, 24.0, 24.2],          # D-1: very tight near 24.0
    }
    for n, members in seeds.items():
        init = (target_dt - timedelta(days=n)).isoformat()
        _seed_init("tokyo", init, target_date, members)
        print(f"  D-{n} init {init}: members={members}")

    # Fake sizing_decisions row
    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-traj",
        "market_name":     "Smoke trajectory",
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

    from weather_decision import _write_phase2_snapshot
    # Stub OM + velocity to keep the test offline
    import markets.open_meteo as om
    import markets.polymarket as pm
    om.get_deterministic_per_model = lambda *a, **kw: []
    pm.get_market_trades            = lambda *a, **kw: []

    _write_phase2_snapshot(sizing_id, "tokyo", target_date, "smoke-cond")

    with db.get_conn() as conn:
        row = conn.execute("""
            SELECT traj_init_d5, traj_spread_d5,
                   traj_init_d4, traj_spread_d4,
                   traj_init_d3, traj_spread_d3,
                   traj_init_d2, traj_spread_d2,
                   traj_init_d1, traj_spread_d1
              FROM decision_snapshots WHERE sizing_decision_id = ?
        """, (sizing_id,)).fetchone()

    print("\n--- trajectory columns ---")
    for k in row.keys():
        v = row[k]
        if v is None:
            print(f"  {k:<20} = NULL")
        else:
            print(f"  {k:<20} = {v:.3f}")

    # Validate: D-5/D-4/D-3/D-1 populated, D-2 NULL
    assert abs(row["traj_init_d5"] - 24.0) < 1e-6, row["traj_init_d5"]
    assert abs(row["traj_init_d4"] - 24.0) < 1e-6
    assert abs(row["traj_init_d3"] - 24.0) < 1e-6
    assert row["traj_init_d2"] is None, "D-2 should be NULL — not seeded"
    assert abs(row["traj_init_d1"] - 24.0) < 1e-6

    # Spreads should monotonically tighten as we approach target
    assert row["traj_spread_d5"] > row["traj_spread_d3"], (row["traj_spread_d5"], row["traj_spread_d3"])
    assert row["traj_spread_d3"] > row["traj_spread_d1"], (row["traj_spread_d3"], row["traj_spread_d1"])
    print("\n  PASS — D-5/D-4/D-3/D-1 populated, D-2 NULL, spreads monotonic")

    # Sidecar writer check — exercise db.save_ensemble_history directly
    print("\n--- save_ensemble_history check ---")
    fake_ensemble = [
        {"date": "2026-06-01", "member_temps": [20.0, 21.0, 22.0]},
        {"date": "2026-06-02", "member_temps": [21.0, 22.0, 23.0]},
    ]
    db.save_ensemble_history("paris", fake_ensemble)
    paris_init = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    got = db.get_ensemble_init("paris", paris_init, "2026-06-01")
    assert got == [20.0, 21.0, 22.0], got
    print(f"  paris init {paris_init} target 2026-06-01: {got}  OK")

    # INSERT OR IGNORE — second write same day should not change row
    db.save_ensemble_history("paris", [{"date": "2026-06-01", "member_temps": [99.0]}])
    got2 = db.get_ensemble_init("paris", paris_init, "2026-06-01")
    assert got2 == [20.0, 21.0, 22.0], f"INSERT OR IGNORE broke: {got2}"
    print(f"  INSERT OR IGNORE preserved first-of-day-wins: {got2}  OK")

    return 0


if __name__ == "__main__":
    sys.exit(main())
