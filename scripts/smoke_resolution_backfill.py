"""Smoke test for weather_catalog.backfill_resolutions().

Seeds weather_city_log rows in several states (with/without condition_id,
past/today logged_date, resolved/unresolved markets), stubs the gamma
resolution lookup, runs backfill_resolutions(), and asserts the writeback
happened only for rows that should resolve.

Coverage:
  1. Past-day row with condition_id + resolved market -> resolved_yes populated.
  2. Past-day row with condition_id + unresolved market -> stays NULL, counted pending.
  3. Past-day row with condition_id + market mid-range -> stays NULL, counted pending.
  4. Past-day row WITHOUT condition_id -> stays NULL, never queried.
  5. Today's row with condition_id + resolved market -> stays NULL (only past days backfilled).
  6. Already-resolved row -> not re-queried (idempotent).
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


def _seed(logged_date: str, city: str, threshold: str,
          condition_id: str | None, resolved_yes: int | None):
    db.upsert_city_log({
        "logged_date":  logged_date,
        "city":         city,
        "market_type":  "threshold",
        "threshold":    threshold,
        "volume_24h":   1000.0,
        "yes_price":    0.5,
        "model_prob":   0.5,
        "ensemble_pct": 0.5,
        "ensemble_n":   71,
        "logged_at":    datetime.now(timezone.utc).isoformat(),
        "condition_id": condition_id,
    })
    if resolved_yes is not None:
        db.mark_city_log_resolved(logged_date, city, threshold, bool(resolved_yes))


def main() -> int:
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    dby       = (today - timedelta(days=2)).isoformat()
    today_s   = today.isoformat()

    # 1. Past-day, has condition_id, market resolves YES
    _seed(yesterday, "tokyo",   ">=27C", "cond-resolved-yes", None)
    # 2. Past-day, has condition_id, market resolves NO
    _seed(yesterday, "paris",   ">=22C", "cond-resolved-no",  None)
    # 3. Past-day, has condition_id, market not yet resolved (still in window)
    _seed(yesterday, "munich",  ">=18C", "cond-unresolved",   None)
    # 4. Past-day, has condition_id, response says resolved but mid-range price
    _seed(yesterday, "chicago", ">=20C", "cond-midrange",     None)
    # 5. Past-day, NO condition_id (legacy row) -> skipped entirely
    _seed(dby,       "berlin",  ">=15C", None,                None)
    # 6. Today's row, has condition_id, market resolved -> not in past-day window
    _seed(today_s,   "wellington", ">=12C", "cond-resolved-yes-today", None)
    # 7. Already-resolved row, has condition_id -> skipped (resolved_yes IS NOT NULL)
    _seed(yesterday, "hong kong", ">=27C", "cond-already-set", 1)

    # Stub get_resolution_status by condition_id
    fake_responses = {
        "cond-resolved-yes":       {"resolved": True,  "yes_price": 1.0},
        "cond-resolved-no":        {"resolved": True,  "yes_price": 0.0},
        "cond-unresolved":         {"resolved": False},
        "cond-midrange":           {"resolved": True,  "yes_price": 0.55},
        "cond-resolved-yes-today": {"resolved": True,  "yes_price": 1.0},
        "cond-already-set":        {"resolved": True,  "yes_price": 1.0},
    }
    queried: list[str] = []

    def fake_resolve(condition_id: str):
        queried.append(condition_id)
        return fake_responses.get(condition_id)

    import weather_catalog
    weather_catalog.get_resolution_status = fake_resolve

    print("\n--- Running backfill_resolutions ---")
    summary = weather_catalog.backfill_resolutions(lookback_days=7)
    print(f"  summary: {summary}")
    print(f"  queried condition_ids: {sorted(set(queried))}")

    # The today_s row and the NULL-condition_id row must NOT have been queried.
    assert "cond-resolved-yes-today" not in queried, "today's row should not be queried"
    # The already-resolved row must NOT have been queried (already non-NULL).
    assert "cond-already-set" not in queried, "already-resolved row should not be re-queried"

    # Assertions on summary counts: 2 updated (yes/no), 2 pending (unresolved + midrange)
    assert summary["updated"] == 2, summary
    assert summary["pending"] == 2, summary
    assert summary["errors"]  == 0, summary

    # Verify DB state
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT city, threshold, resolved_yes, condition_id
              FROM weather_city_log
             ORDER BY city, threshold
        """).fetchall()

    actual = {(r["city"], r["threshold"]): r["resolved_yes"] for r in rows}
    expected = {
        ("tokyo",      ">=27C"): 1,        # resolved YES
        ("paris",      ">=22C"): 0,        # resolved NO
        ("munich",     ">=18C"): None,     # unresolved
        ("chicago",    ">=20C"): None,     # midrange
        ("berlin",     ">=15C"): None,     # NULL condition_id
        ("wellington", ">=12C"): None,     # today's row, skipped
        ("hong kong",  ">=27C"): 1,        # already resolved, untouched
    }
    for k, v in expected.items():
        assert actual.get(k) == v, f"{k}: expected {v}, got {actual.get(k)}"
    print("  PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
