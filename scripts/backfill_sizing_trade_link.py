"""Backfill sizing_decisions.trade_id for rows stranded before the executor
link fix (commit 2026-05-18, D1 of unified-path-forward plan).

Match logic (per [tasks/findings/2026-05-18_platt_refit_v1_validation.md] §
'Discovered bug: sizing_decisions.trade_id is NULL on every row'):
  - same market_id
  - same direction (lowercase compared)
  - |sizing_decisions.recorded_at - trades.opened_at| < 60s

If a sizing_decisions row has multiple candidate trades within 60s, pick the
closest by absolute time-delta and warn.

Idempotent:
  - Only acts on rows where sizing_decisions.trade_id IS NULL.
  - Re-running after a successful pass is a no-op.

Run on the production host (Kamatera):
    python scripts/backfill_sizing_trade_link.py             # report-only
    python scripts/backfill_sizing_trade_link.py --apply     # write updates
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Handles both naive ISO and trailing Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main(apply_changes: bool) -> None:
    print(f"=== sizing_decisions.trade_id backfill ===")
    print(f"Mode: {'APPLY (write)' if apply_changes else 'REPORT-ONLY (dry run)'}\n")

    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, market_id, direction, recorded_at
            FROM sizing_decisions
            WHERE trade_id IS NULL
            ORDER BY recorded_at
        """).fetchall()
        rows = [dict(r) for r in rows]

    n_total = len(rows)
    print(f"sizing_decisions with NULL trade_id: {n_total:,}\n")
    if n_total == 0:
        print("Nothing to backfill. Done.")
        return

    linked = 0
    ambiguous = 0
    unmatched = 0

    for sd in rows:
        sd_id  = sd["id"]
        mid    = sd["market_id"]
        sd_dir = (sd["direction"] or "").lower()
        ts_str = sd["recorded_at"]
        ts     = _parse_ts(ts_str)
        if not (mid and sd_dir and ts):
            unmatched += 1
            print(f"  [skip] sd_id={sd_id}: missing market_id/direction/recorded_at")
            continue

        with db.get_conn() as conn:
            trades = conn.execute("""
                SELECT id, direction, opened_at
                FROM trades
                WHERE market_id = ?
            """, (mid,)).fetchall()
        trades = [dict(t) for t in trades]
        candidates: list[tuple[float, int]] = []
        for t in trades:
            if (t.get("direction") or "").lower() != sd_dir:
                continue
            t_ts = _parse_ts(t.get("opened_at"))
            if t_ts is None:
                continue
            try:
                delta = abs((t_ts - ts).total_seconds())
            except Exception:
                continue
            if delta <= 60.0:
                candidates.append((delta, t["id"]))

        if not candidates:
            unmatched += 1
            print(f"  [no-match] sd_id={sd_id}  market={mid[:14]}  dir={sd_dir}  "
                  f"ts={ts_str}")
            continue

        candidates.sort()
        best_delta, best_trade_id = candidates[0]
        if len(candidates) > 1:
            ambiguous += 1
            print(f"  [ambiguous] sd_id={sd_id}  picking trade_id={best_trade_id} "
                  f"(dt={best_delta:.1f}s) over {len(candidates)-1} other(s)")

        if apply_changes:
            db.link_sizing_decision_to_trade(int(sd_id), int(best_trade_id))
        linked += 1
        print(f"  [{'linked' if apply_changes else 'would-link'}] "
              f"sd_id={sd_id}  ->  trade_id={best_trade_id}  dt={best_delta:.1f}s")

    print(f"\nResults:")
    print(f"  total stranded: {n_total:,}")
    print(f"  linked:         {linked:,}")
    print(f"  ambiguous:      {ambiguous:,} (closest match used)")
    print(f"  no match:       {unmatched:,}")
    if not apply_changes:
        print(f"\n(REPORT-ONLY — re-run with --apply to write the UPDATEs.)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                         help="Write UPDATEs. Without this flag, runs report-only.")
    args = parser.parse_args()
    main(apply_changes=args.apply)
