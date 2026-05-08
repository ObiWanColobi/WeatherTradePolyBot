"""Phase2-06 local smoke test — flip detector + DB write path.

Drives FlipDetector with synthetic price ticks across two simulated markets
and validates:
  1. No fire below threshold
  2. YES_flip when price rises >= threshold within window
  3. NO_flip when price falls >= threshold within window
  4. Cooldown suppresses repeat fires
  5. Window expiry — old reference points drop out
  6. write_flip_event lands a row visible in flip_events

Time is injected via observe(now_ts=...) so the test runs in <1s.
"""

from __future__ import annotations

import os
import sys
import tempfile
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

from markets.flip_detector import FlipDetector  # noqa: E402


def run_unit() -> bool:
    print("\n--- Unit checks ---")
    fd = FlipDetector(threshold_pct=0.05, window_min=60.0, cooldown_sec=300.0)

    # 1. Below threshold — no fire
    t0 = 1_000_000.0
    assert fd.observe("M1", 0.50, now_ts=t0)        is None
    assert fd.observe("M1", 0.52, now_ts=t0 + 60)   is None
    assert fd.observe("M1", 0.54, now_ts=t0 + 120)  is None  # 0.04 < 0.05
    print("  below-threshold: no fire  OK")

    # 2. YES_flip — push delta past threshold
    ev = fd.observe("M1", 0.56, now_ts=t0 + 180)    # 0.06 vs 0.50
    assert ev is not None and ev["direction"] == "YES_flip", ev
    assert abs(ev["delta_pct"] - 0.06) < 1e-9, ev
    print(f"  YES_flip:    {ev}  OK")

    # 3. Cooldown — within 300s of fire, no further fire even if delta huge
    ev2 = fd.observe("M1", 0.20, now_ts=t0 + 240)   # cooldown active
    assert ev2 is None, ev2
    print("  cooldown:    suppressed  OK")

    # 4. Post-cooldown NO_flip
    ev3 = fd.observe("M1", 0.40, now_ts=t0 + 600)   # well past cooldown
    # Reference is the post-fire 0.56. 0.40 - 0.56 = -0.16
    assert ev3 is not None and ev3["direction"] == "NO_flip", ev3
    print(f"  NO_flip:     {ev3}  OK")

    # 5. Window expiry — separate market, walk the reference past 60min
    fd2 = FlipDetector(threshold_pct=0.05, window_min=60.0, cooldown_sec=10.0)
    t = 2_000_000.0
    assert fd2.observe("M2", 0.30, now_ts=t)            is None
    assert fd2.observe("M2", 0.32, now_ts=t + 30 * 60)  is None  # +30min, +0.02
    # 70min later — original 0.30 should drop out of window
    ev4 = fd2.observe("M2", 0.36, now_ts=t + 70 * 60)
    # Reference is now 0.32 (the only entry inside the 60min window when we tick)
    # 0.36 - 0.32 = 0.04 < 0.05 -> no fire
    assert ev4 is None, ev4
    print("  window-expiry: ref dropped out  OK")

    # 6. DB write path
    db.write_flip_event({
        "detected_at":    datetime.now(timezone.utc).isoformat(),
        "market_id":      "smoke-flip-1",
        "city":           "tokyo",
        "direction":      "YES_flip",
        "price_before":   0.50,
        "price_after":    0.58,
        "delta_pct":      0.08,
        "minutes_window": 12.5,
        "poll_tick_id":   "42",
    })
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM flip_events WHERE market_id = 'smoke-flip-1'"
        ).fetchone()
    assert row is not None
    assert row["direction"] == "YES_flip"
    assert row["city"] == "tokyo"
    assert abs(row["delta_pct"] - 0.08) < 1e-9
    print(f"  db row:      {dict(row)}  OK")

    print("  PASS")
    return True


if __name__ == "__main__":
    run_unit()
