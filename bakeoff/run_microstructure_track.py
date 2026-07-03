# bakeoff/run_microstructure_track.py
"""Microstructure-track bake-off runner (depth-aware).

Unlike the forecast track, this track needs REAL captured orderbook depth. Depth was
only logged from 2026-06-25 onward, and only ~27-31% of rows carry a populated
`orderbook_bids_json` / `orderbook_asks_json`. The clean, truth-gradeable AND
depth-covered resolution-date window is 2026-06-26..2026-07-02 inclusive:
  - >= 06-26: depth actually present (05-xx and 06-25 are 0% depth -> excluded)
  - <= 07-02: METAR truth has settled (07-03 is "today"; the loader already drops
    resolution_date >= today, and future dates have no truth anyway).

This runner:
  1. Loads city-days from the parquet rollup (depth columns now flow through the loader
     after the snapshot_replay._SNAPSHOT_COLS fix + decision._FIELDS fix).
  2. Restricts to city-days whose resolution_date is in the depth window AND that have
     >= 1 fire-time decision-row carrying populated depth JSON.
  3. Picks the fire-time decision snapshot (decision.decision_rows, default 12h before close).
  4. Exposes run_candidate(candidate_module, fee_rate) -> scorer.score(...) so later
     agents can score any candidate that follows the interface.Candidate protocol.

HONESTY: the runner only surfaces depth that was captured; candidates must walk that
depth via harness.depth / harness.cost_model and never invent liquidity beyond the
captured levels. Truth is only touched at settlement (scorer), never at decision time.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bakeoff.harness.loader import load_city_days, CityDay  # noqa: E402
from bakeoff.harness.decision import decision_rows  # noqa: E402
from bakeoff.harness.scorer import score  # noqa: E402

PARQUET_DIR = "research_db/snapshot_parquet_rebuild"

# Inclusive resolution-date window with BOTH captured depth AND settled METAR truth.
DEPTH_WINDOW_MIN = "2026-06-26"
DEPTH_WINDOW_MAX = "2026-07-02"

# Default fire window (hours before close) for picking the decision snapshot.
FIRE_HOURS_BEFORE = 12


def _has_depth(val) -> bool:
    """True iff an orderbook JSON cell is a non-empty captured ladder string."""
    if val is None:
        return False
    try:
        import pandas as pd  # local import; keep module import cheap
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    # Empty string, 'null', or an empty JSON list are all "no depth captured".
    return s not in ("", "null", "None", "[]")


def _row_has_depth(row: dict) -> bool:
    return _has_depth(row.get("orderbook_bids_json")) or _has_depth(row.get("orderbook_asks_json"))


def _in_window(res_date: str) -> bool:
    return DEPTH_WINDOW_MIN <= str(res_date)[:10] <= DEPTH_WINDOW_MAX


def load_depth_city_days(
    parquet_dir: str = PARQUET_DIR,
    hours_before_close: int = FIRE_HOURS_BEFORE,
) -> list[tuple[CityDay, str, list[dict]]]:
    """Return [(city_day, fire_ts, decision_rows), ...] for depth-covered, truth-gradeable
    city-days that carry >= 1 fire-time decision-row with populated depth JSON.

    Each tuple's decision rows are exactly the fire-time snapshot the runner will hand a
    candidate, so downstream scoring never re-picks a different snapshot (no lookahead
    drift between selection and evaluation).
    """
    all_cds = load_city_days(parquet_dir)
    out: list[tuple[CityDay, str, list[dict]]] = []
    for cd in all_cds:
        if not _in_window(cd.resolution_date):
            continue
        if cd.truth_f is None:  # need settled METAR truth to grade
            continue
        ts, rows = decision_rows(cd.snapshots, hours_before_close)
        if not ts or not rows:
            continue
        if not any(_row_has_depth(r) for r in rows):
            continue
        out.append((cd, ts, rows))
    return out


def _depth_plumbing_stats(all_cds, hours_before_close: int) -> dict:
    """Truth-agnostic view: how much captured depth reaches decision rows in the window.

    Separates the DEPTH-PLUMBING success (columns flow through loader -> decision rows)
    from the GRADEABLE count (which additionally requires settled METAR truth). Lets the
    coverage report show the plumbing works even while truth for the window is still
    pending ingestion.
    """
    in_win = [cd for cd in all_cds if _in_window(cd.resolution_date)]
    n_depth_cd = 0
    n_depth_rows = 0
    sample_cols: list[str] = []
    for cd in in_win:
        ts, rows = decision_rows(cd.snapshots, hours_before_close)
        if not ts or not rows:
            continue
        dr = [r for r in rows if _row_has_depth(r)]
        if dr:
            n_depth_cd += 1
            n_depth_rows += len(dr)
            if not sample_cols:
                sample_cols = sorted(dr[0].keys())
    return {
        "n_city_days_in_window": len(in_win),
        "n_city_days_with_depth_rows": n_depth_cd,
        "n_decision_rows_with_depth": n_depth_rows,
        "decision_row_fields": sample_cols,
    }


def coverage_report(
    parquet_dir: str = PARQUET_DIR,
    hours_before_close: int = FIRE_HOURS_BEFORE,
) -> dict:
    """Coverage report: separates depth-plumbing coverage from truth-gated gradeable count.

    - Depth plumbing (truth-agnostic): captured orderbook JSON reaching decision rows.
    - Gradeable (truth-gated): city-days that ALSO have settled METAR truth, i.e. what
      run_candidate() will actually score. If METAR truth for the depth window has not
      been ingested into research.duckdb yet, this is 0 even though depth is present.
    """
    all_cds = load_city_days(parquet_dir)
    plumbing = _depth_plumbing_stats(all_cds, hours_before_close)

    # Gradeable subset (truth-gated): reuse the same window + depth filters, add truth.
    triples = [
        (cd, ts, rows)
        for cd in all_cds
        if _in_window(cd.resolution_date) and cd.truth_f is not None
        for ts, rows in [decision_rows(cd.snapshots, hours_before_close)]
        if ts and rows and any(_row_has_depth(r) for r in rows)
    ]
    cities = sorted({cd.city for cd, _, _ in triples})
    n_graded_depth_rows = sum(
        sum(1 for r in rows if _row_has_depth(r)) for _, _, rows in triples
    )

    rep = {
        "depth_window": f"{DEPTH_WINDOW_MIN}..{DEPTH_WINDOW_MAX}",
        "fire_hours_before": hours_before_close,
        # gradeable (truth-gated) — what run_candidate scores:
        "n_city_days_with_depth": len(triples),
        "n_cities": len(cities),
        "cities": cities,
        "n_decision_rows_with_depth": n_graded_depth_rows,
        # depth plumbing (truth-agnostic) — proves the columns flow end-to-end:
        "depth_plumbing": plumbing,
        "truth_covered": len(triples) > 0,
    }
    print(
        f"[microstructure] depth window {DEPTH_WINDOW_MIN}..{DEPTH_WINDOW_MAX}: "
        f"gradeable {len(triples)} city-days / {len(cities)} cities "
        f"({n_graded_depth_rows} depth decision-rows); "
        f"depth-plumbing {plumbing['n_city_days_with_depth_rows']} city-days / "
        f"{plumbing['n_decision_rows_with_depth']} depth decision-rows"
    )
    if not rep["truth_covered"]:
        print(
            "[microstructure] NOTE: 0 gradeable — METAR truth for the depth window is not "
            "yet in research.duckdb (truth ends 2026-06-14/15). Depth plumbing is verified "
            "working; ingest truth for 2026-06-26..07-02 before scoring candidates."
        )
    return rep


def run_candidate(candidate_module, fee_rate: float, *,
                  parquet_dir: str = PARQUET_DIR,
                  hours_before_close: int = FIRE_HOURS_BEFORE,
                  params: dict | None = None) -> dict:
    """Score a candidate on the depth-covered universe.

    candidate_module: any module/object with .evaluate(city_day, fee_rate, params, ...)
      per bakeoff.harness.interface.Candidate. It is handed the fire-time decision rows
      (already selected here) via a `decision_snapshots=` kwarg when its signature accepts
      one; otherwise it may re-derive them from city_day.snapshots.
    fee_rate: 0.0 (optimistic) or 0.05 (pessimistic PESSIMISTIC_FEE); a real edge must
      survive the pessimistic rate.

    Returns scorer.score(trades, truth_by_key) — ROI, per-city robustness, clustered
    bootstrap CI. A positive result is a forward-shadow candidate, NOT proof.
    """
    import inspect

    triples = load_depth_city_days(parquet_dir, hours_before_close)
    trades: list[dict] = []

    # Only pass decision_snapshots if the candidate's evaluate() accepts it.
    try:
        sig = inspect.signature(candidate_module.evaluate)
        accepts_snaps = "decision_snapshots" in sig.parameters
    except (TypeError, ValueError):
        accepts_snaps = False

    for cd, _ts, rows in triples:
        if accepts_snaps:
            legs = candidate_module.evaluate(cd, fee_rate, params, decision_snapshots=rows)
        else:
            legs = candidate_module.evaluate(cd, fee_rate, params)
        if legs:
            trades += legs

    truth_by_key = {(cd.city, cd.resolution_date): cd.truth_f for cd, _, _ in triples}
    return score(trades, truth_by_key)


if __name__ == "__main__":
    import json
    print(json.dumps(coverage_report(), indent=2, default=str))
