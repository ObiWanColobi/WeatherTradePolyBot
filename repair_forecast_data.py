"""
repair_forecast_data.py
-----------------------
One-time repair script for corrupted forecast_correct, actual_resolution,
and was_correct data caused by a bug in weather_calibration.py.

Bug summary:
  For NO-direction trades backfilled by the CLOB calibration pass, the stored
  token_id was the NO token. The CLOB midpoint returned the NO token price.
  That price was treated as the YES token price, so actual_resolution and
  forecast_correct were computed from the wrong perspective.

  Correct fix: for NO trades, YES price = 1.0 - resolution_price (NO token price).

Usage:
  python repair_forecast_data.py           # dry run (shows changes, writes nothing)
  python repair_forecast_data.py --apply   # writes changes atomically
  python repair_forecast_data.py --dry-run # explicit dry run
"""

import sqlite3
import sys

DB_PATH = "f:/CodeProjects/TestCode1/weather_bot.db"

# Resolution thresholds — match weather_calibration.py
_CLOB_YES = 0.98
_CLOB_NO  = 0.02


def compute_actual_resolution(yes_price: float) -> str | None:
    """
    Convert a YES-token price to an actual_resolution label.
    Returns None if the price is in the intermediate settling range.
    """
    if yes_price >= _CLOB_YES:
        return "YES"
    if yes_price <= _CLOB_NO:
        return "NO"
    return None  # still settling — not resolved


def repair(apply: bool = False) -> None:
    mode_label = "APPLY" if apply else "DRY RUN (use --apply to write changes)"
    print(f"=== {mode_label} ===")
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    trades_fixed = 0
    tf_fixed = 0

    # ── Build corrected resolution map from trades ────────────────────────────
    # market_id -> corrected actual_resolution (for use in trader_forecasts repair)
    corrected_resolutions: dict[str, str] = {}

    try:
        with conn:
            # ── Phase 1: Repair trades table ──────────────────────────────────
            #
            # Targets: closed trades with actual_resolution already set, where the
            # exit_reason does NOT contain "resolved" (i.e. backfilled by calibration,
            # not settled by paper.py's settle_resolved which handles inversion correctly).
            #
            # For YES trades: resolution_price IS the YES token price — no change needed.
            # For NO  trades: resolution_price is the NO token price; must invert it.

            candidate_trades = conn.execute("""
                SELECT id, direction, market_id, market_name, city, threshold,
                       actual_resolution, forecast_correct, resolution_price
                  FROM trades
                 WHERE status = 'closed'
                   AND actual_resolution IS NOT NULL
                   AND exit_reason NOT LIKE '%resolved%'
            """).fetchall()

            for trade in candidate_trades:
                trade_id        = trade["id"]
                direction       = (trade["direction"] or "").upper()
                stored_res      = trade["actual_resolution"]
                stored_correct  = trade["forecast_correct"]
                res_price       = trade["resolution_price"]
                market_name     = trade["market_name"] or ""
                city            = trade["city"] or "?"
                threshold       = trade["threshold"] or ""
                market_id       = trade["market_id"]

                label = f"{city} {direction} {threshold}".strip()

                if direction == "NO":
                    # resolution_price stores the NO token price; convert to YES price
                    if res_price is None:
                        print(f"  Trade #{trade_id} {label}: SKIP — resolution_price is NULL")
                        continue
                    corrected_yes = 1.0 - res_price
                    new_actual = compute_actual_resolution(corrected_yes)
                    if new_actual is None:
                        print(f"  Trade #{trade_id} {label}: SKIP — corrected YES price "
                              f"{corrected_yes:.4f} is intermediate (still settling?)")
                        continue
                elif direction == "YES":
                    # resolution_price is already the YES token price; verify only
                    if res_price is None:
                        print(f"  Trade #{trade_id} {label}: SKIP — resolution_price is NULL")
                        continue
                    corrected_yes = res_price
                    new_actual = compute_actual_resolution(corrected_yes)
                    if new_actual is None:
                        print(f"  Trade #{trade_id} {label}: SKIP — YES price "
                              f"{corrected_yes:.4f} is intermediate")
                        continue
                else:
                    print(f"  Trade #{trade_id} {label}: SKIP — unknown direction {direction!r}")
                    continue

                new_correct = 1 if direction == new_actual else 0

                # Record corrected resolution for trader_forecasts phase
                corrected_resolutions[market_id] = new_actual

                # Only report/update rows that actually differ
                actual_changed  = (new_actual  != stored_res)
                correct_changed = (new_correct != stored_correct)

                if not actual_changed and not correct_changed:
                    # Already correct — nothing to do
                    continue

                # Build change description
                parts = []
                if actual_changed:
                    parts.append(f"actual_resolution {stored_res}->{new_actual}")
                if correct_changed:
                    parts.append(f"forecast_correct {stored_correct}->{new_correct}")

                print(f"  Trade #{trade_id} {label}: {', '.join(parts)}")
                trades_fixed += 1

                if apply:
                    conn.execute("""
                        UPDATE trades
                           SET actual_resolution = ?,
                               forecast_correct  = ?
                         WHERE id = ?
                    """, (new_actual, new_correct, trade_id))

            # ── Phase 2: Repair trader_forecasts table ────────────────────────
            #
            # For each trader_forecast row:
            #   1. Look up the corrected actual_resolution from the trades we just fixed.
            #   2. If no matching trade, fall back to the resolution_price stored in
            #      trader_forecasts itself (also came from buggy calibration for NO trades).
            #   3. Recompute was_correct.

            tf_rows = conn.execute("""
                SELECT id, wallet, market_id, direction, city,
                       actual_resolution, resolution_price, was_correct
                  FROM trader_forecasts
                 WHERE actual_resolution IS NOT NULL
            """).fetchall()

            for tf in tf_rows:
                tf_id          = tf["id"]
                wallet         = tf["wallet"] or ""
                market_id      = tf["market_id"]
                direction      = (tf["direction"] or "").upper()
                stored_actual  = tf["actual_resolution"]
                stored_correct = tf["was_correct"]
                res_price      = tf["resolution_price"]
                city           = tf["city"] or "?"

                wallet_short = wallet[:10] if len(wallet) > 10 else wallet

                # Prefer the corrected resolution from the trades repair pass
                if market_id in corrected_resolutions:
                    new_actual = corrected_resolutions[market_id]
                else:
                    # No trade match — use resolution_price in trader_forecasts.
                    # This price came from freeze_trader_forecasts which received the
                    # close_price directly from calibration. For NO-direction forecasts
                    # coming from NO-direction bot trades, the price is the NO token price.
                    # However, freeze_trader_forecasts uses close_price > 0.5 to determine
                    # actual_resolution, not direction, so if the price was already the YES
                    # token price when passed in, the stored actual_resolution is correct.
                    #
                    # We can only correct rows where we have a corrected trade match.
                    # Without a trade match, leave it alone to avoid making things worse.
                    continue

                new_correct = 1 if direction == new_actual else 0

                actual_changed  = (new_actual  != stored_actual)
                correct_changed = (new_correct != stored_correct)

                if not actual_changed and not correct_changed:
                    continue

                parts = []
                if actual_changed:
                    parts.append(f"was_correct {stored_correct}->{new_correct}")
                if correct_changed and not actual_changed:
                    parts.append(f"was_correct {stored_correct}->{new_correct}")

                label = f"{city} {direction}"
                print(f"  Trader forecast wallet={wallet_short}... {label}: "
                      f"actual_resolution {stored_actual}->{new_actual}, "
                      f"was_correct {stored_correct}->{new_correct}")
                tf_fixed += 1

                if apply:
                    conn.execute("""
                        UPDATE trader_forecasts
                           SET actual_resolution = ?,
                               was_correct       = ?
                         WHERE id = ?
                    """, (new_actual, new_correct, tf_id))

            if not apply:
                # Rollback so nothing is written even if we accidentally executed DML
                conn.execute("SELECT 1")  # keep connection alive; context manager commits
                # We wrap in a transaction block — raise to rollback
                raise _DryRunAbort()

    except _DryRunAbort:
        pass  # expected in dry-run mode
    finally:
        conn.close()

    print()
    print(f"Summary: {trades_fixed} trade(s) fixed, {tf_fixed} trader_forecast(s) fixed")
    if not apply:
        print("(No changes written — run with --apply to commit)")
    else:
        print("Changes committed.")


class _DryRunAbort(Exception):
    """Sentinel used to abort the transaction in dry-run mode."""


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--apply" in args:
        apply = True
    elif "--dry-run" in args or not args:
        apply = False
    else:
        print(f"Unknown argument(s): {args - {'--apply', '--dry-run'}}")
        print("Usage: python repair_forecast_data.py [--dry-run | --apply]")
        sys.exit(1)

    repair(apply=apply)
