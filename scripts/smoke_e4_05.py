"""E4-05 local smoke test — prev-day same-city resolution lookup.

Seeds weather_city_log with a few city/date/threshold rows, then drives
_write_phase2_snapshot and verifies prev_day_outcome + prev_day_question
on the resulting decision_snapshots row.

Coverage:
  1. Recent resolved row exists -> outcome populated.
  2. No resolved row -> NULL.
  3. Multiple rows same date -> highest-volume threshold picked.
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


def _seed_log(logged_date: str, city: str, threshold: str, vol: float, resolved_yes: int | None):
    db.upsert_city_log({
        "logged_date":  logged_date,
        "city":         city,
        "market_type":  "threshold",
        "threshold":    threshold,
        "volume_24h":   vol,
        "yes_price":    0.5,
        "model_prob":   0.5,
        "ensemble_pct": 0.5,
        "ensemble_n":   71,
        "logged_at":    datetime.now(timezone.utc).isoformat(),
    })
    if resolved_yes is not None:
        db.mark_city_log_resolved(logged_date, city, threshold, bool(resolved_yes))


def _check_unit() -> bool:
    print("\n--- Unit checks on get_prev_day_resolution ---")

    # tokyo: yesterday resolved YES on >=27C, day-before-yesterday NO on >=25C
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    dby       = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    today     = datetime.now(timezone.utc).date().isoformat()

    _seed_log(yesterday, "tokyo", ">=27C", 5000.0, 1)
    _seed_log(dby,       "tokyo", ">=25C",  500.0, 0)
    # Two thresholds same date — higher volume should win
    _seed_log(yesterday, "paris", ">=20C",  100.0, 0)
    _seed_log(yesterday, "paris", ">=22C", 9000.0, 1)
    # Unresolved rows should be skipped
    _seed_log(yesterday, "berlin", ">=18C", 8000.0, None)

    # 1. tokyo before today -> yesterday >=27C YES
    r = db.get_prev_day_resolution("tokyo", today)
    assert r is not None and r["resolved_yes"] == 1 and r["threshold"] == ">=27C", r
    print(f"  tokyo: {r}  OK")

    # 2. paris before today -> highest-volume threshold
    r = db.get_prev_day_resolution("paris", today)
    assert r is not None and r["threshold"] == ">=22C" and r["resolved_yes"] == 1, r
    print(f"  paris: {r}  OK")

    # 3. berlin: only unresolved row -> None
    r = db.get_prev_day_resolution("berlin", today)
    assert r is None, r
    print(f"  berlin: {r}  OK (no resolved row)")

    # 4. seattle: never logged -> None
    r = db.get_prev_day_resolution("seattle", today)
    assert r is None, r
    print(f"  seattle: {r}  OK (city not logged)")

    print("  PASS")
    return True


def _check_end_to_end() -> int:
    print("\n--- End-to-end via _write_phase2_snapshot ---")
    from weather_decision import _write_phase2_snapshot
    # Stub the OM call so we don't make a real network request — we only
    # care about the prev-day branch here.
    import markets.open_meteo as om
    om.get_deterministic_per_model = lambda *a, **kw: []

    # tokyo decision today, market resolves "today"; seeded yesterday=YES
    today = datetime.now(timezone.utc).date().isoformat()
    sizing_id = db.write_sizing_decision({
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "market_id":       "smoke-test-e4-05",
        "market_name":     "Smoke E4-05",
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

    _write_phase2_snapshot(sizing_id, "tokyo", today)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT prev_day_outcome, prev_day_question "
            "FROM decision_snapshots WHERE sizing_decision_id = ?",
            (sizing_id,),
        ).fetchone()
    print(f"  row: prev_day_outcome={row['prev_day_outcome']!r} prev_day_question={row['prev_day_question']!r}")
    assert row["prev_day_outcome"] == "YES", row["prev_day_outcome"]
    assert row["prev_day_question"] == ">=27C", row["prev_day_question"]
    print("  PASS")
    return 0


if __name__ == "__main__":
    _check_unit()
    sys.exit(_check_end_to_end())
