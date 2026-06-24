# bakeoff/run_forecast_track.py
"""Forecast-track bake-off: fit EMOS, gate, score candidate #3 at the ask on the gradeable universe."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bakeoff.harness.loader import load_city_days, time_split
from bakeoff.harness.decision import decision_rows
from bakeoff.harness import report
from bakeoff.harness.scorer import score
from bakeoff.forecast import loader as floader
from bakeoff.forecast.emos import fit_emos
from bakeoff.harness.calibration_gate import calibration_gate
from bakeoff.candidates import c3_forecast_taker as c3

FIRE_HOURS_BEFORE = 16  # earlier fire = more lead time, wider spreads = more conservative test


def build_records(city_days, gefs_df):
    recs = []
    for cd in city_days:
        if cd.truth_f is None:
            continue
        ts, _rows = decision_rows(cd.snapshots, FIRE_HOURS_BEFORE)
        if not ts:
            continue
        members = floader.members_asof(gefs_df, floader.canon_city(cd.city),
                                       cd.resolution_date, ts)
        if not members:
            continue
        recs.append({"member_temps": members, "truth_f": cd.truth_f})
    return recs


def run(parquet_dir):
    city_days = [cd for cd in load_city_days(parquet_dir) if cd.truth_f is not None]
    cities = sorted({cd.city for cd in city_days})
    gefs_df = floader.load_ensemble_members(cities)
    tune, holdout = time_split(city_days)

    tune_recs = build_records(tune, gefs_df)
    params = fit_emos(tune_recs)

    holdout_recs = build_records(holdout, gefs_df)
    # raw-ensemble CRPS baseline: sigma from raw member spread (no inflation)
    import numpy as np
    raw_sigmas = [float(np.std(r["member_temps"]) or 1.0) for r in holdout_recs]
    from bakeoff.forecast.emos import _gaussian_crps
    raw_mu = np.array([float(np.mean(r["member_temps"])) for r in holdout_recs])
    raw_sd = np.array(raw_sigmas)
    raw_y = np.array([r["truth_f"] for r in holdout_recs])
    raw_crps = float(np.mean(_gaussian_crps(raw_mu, raw_sd, raw_y))) if holdout_recs else 1e9

    gate = calibration_gate(holdout_recs, params, raw_crps)
    universe = {"n_cities": len(cities), "n_graded_city_days": len(city_days),
                "n_holdout_records": len(holdout_recs)}

    # Diagnostics for the raw-CRPS floor (std<0.1 -> the `or 1.0` floor is doing the work).
    n_near_zero_spread = sum(1 for r in holdout_recs
                             if float(np.std(r["member_temps"])) < 0.1)
    diag = {"raw_crps": raw_crps, "emos_crps": gate["emos_crps"],
            "n_near_zero_spread": n_near_zero_spread,
            "n_holdout_records": len(holdout_recs)}

    if not gate["passed"]:
        rows = [report.scorecard_row(c3.NAME, "Calibration gate",
                {"roi": 0.0}, {"roi": 0.0, "city_majority_positive": False,
                 "bootstrap_ci_low": 0.0, "n_trades": 0}, gate)]
        report.write_verdict(rows, "bakeoff/results/verdict_forecast_track.md")
        return {"disqualified": True, "gate": gate, "universe": universe, "diag": diag}

    def score_regime(fee_rate):
        trades = []
        for cd in holdout:
            if cd.truth_f is None:
                continue
            ts, rows = decision_rows(cd.snapshots, FIRE_HOURS_BEFORE)
            if not ts:
                continue
            members = floader.members_asof(gefs_df, floader.canon_city(cd.city),
                                           cd.resolution_date, ts)
            if not members:
                continue
            trades += c3.evaluate(cd, fee_rate, params,
                                  member_temps=members, decision_snapshots=rows)
        truth_by_key = {(cd.city, cd.resolution_date): cd.truth_f for cd in holdout}
        return score(trades, truth_by_key)

    from bakeoff.harness.cost_model import OPTIMISTIC_FEE, PESSIMISTIC_FEE
    opt = score_regime(OPTIMISTIC_FEE)
    pess = score_regime(PESSIMISTIC_FEE)
    rows = [report.scorecard_row(c3.NAME, "Stage 3 (held-out)", opt, pess, gate)]
    report.write_verdict(rows, "bakeoff/results/verdict_forecast_track.md")
    return {"disqualified": False, "gate": gate, "universe": universe,
            "diag": diag, "opt": opt, "pess": pess}


if __name__ == "__main__":
    import json, os
    pdir = os.environ.get("SNAPSHOT_PARQUET_DIR", "research_db/snapshot_parquet_rebuild")
    result = run(pdir)
    print(json.dumps({k: v for k, v in result.items() if k != "gate"}, indent=2, default=str))
    # Surface the calibration gate and raw-CRPS-floor diagnostics explicitly.
    print("\n=== calibration gate ===")
    print(json.dumps(result.get("gate", {}), indent=2, default=str))
    diag = result.get("diag", {})
    nz = diag.get("n_near_zero_spread", 0)
    nh = diag.get("n_holdout_records", 0)
    print("\n=== raw-CRPS-floor diagnostics ===")
    print(f"holdout records: {nh}")
    print(f"near-zero raw spread (std<0.1): {nz}"
          + (f" ({nz / nh:.1%})" if nh else ""))
    print(f"raw_crps (floored sigma): {diag.get('raw_crps')}")
    print(f"emos_crps: {diag.get('emos_crps')}")
