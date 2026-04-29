"""
Repair trade #39 (Bug #23): Shanghai NO position recorded as -100% loss
when it actually won and was redeemed externally for $80.01.

Background:
  Bot was offline when the Shanghai market resolved early. The 80 NO shares
  were redeemed on-chain (proceeds $80.01 hit the wallet — confirmed by
  startup balance_sync delta). On restart, the resolver skipped the trade
  because `now < end_date_eod` and reconcile's `_close_stale_position`
  fell through to a midpoint estimate that returned ~0 for the resolved
  orderbook, producing a phantom -$75.46 loss.

  See executor/live.py:_close_stale_position fix and weather_resolver.py
  hours_to_end gate for the root-cause fixes. This script restores the
  affected DB row and removes the phantom-dip rows from balance_history.

Run: python scripts/repair_bug23_trade39.py
"""

import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "weather_bot.db")
DB_PATH = os.path.abspath(DB_PATH)

# Actual redemption observed on Polymarket
REDEEM_PROCEEDS = 80.01  # $ paid out for 80.278363 shares

PHANTOM_DIP_ID_RANGE = (14881, 14893)  # CLOB transient-zero rows on 04-29


def repair_trade_39(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    row = cur.execute("SELECT * FROM trades WHERE id = 39").fetchone()
    if row is None:
        print("ERROR: trade #39 not found")
        sys.exit(1)
    row = dict(row)

    if row.get("exit_reason") != "sold externally (reconciled)":
        print(f"WARN: trade #39 exit_reason is {row.get('exit_reason')!r} — "
              f"not the expected 'sold externally (reconciled)'. Skipping.")
        return

    if row.get("direction", "").upper() != "NO":
        print(f"ERROR: trade #39 direction is {row.get('direction')!r}, "
              f"expected NO")
        sys.exit(1)

    cost = float(row["size_usdc"])
    shares = float(row["shares"])
    proceeds = REDEEM_PROCEEDS
    pnl = proceeds - cost
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
    exit_price = proceeds / shares if shares > 0 else 1.0

    print(f"Before: pnl=${row['pnl']:+.2f} ({row['pnl_pct']:+.2f}%) "
          f"exit_price={row['exit_price']} exit_reason={row['exit_reason']!r}")
    print(f"After:  pnl=${pnl:+.2f} ({pnl_pct:+.2f}%) "
          f"exit_price={exit_price:.4f} exit_reason='resolved (reconciled)'")

    cur.execute("""
        UPDATE trades SET
            exit_price        = ?,
            pnl               = ?,
            pnl_pct           = ?,
            exit_reason       = 'resolved (reconciled)',
            actual_resolution = 'NO',
            forecast_correct  = 1,
            resolution_price  = 0.0,
            claim_status      = 'claimed_externally'
        WHERE id = 39
    """, (exit_price, pnl, pnl_pct))

    print(f"Updated rows: {cur.rowcount}")


def clean_balance_history(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    lo, hi = PHANTOM_DIP_ID_RANGE
    rows = cur.execute(
        "SELECT id, amount, recorded_at FROM balance_history "
        "WHERE id BETWEEN ? AND ? ORDER BY id",
        (lo, hi),
    ).fetchall()

    if not rows:
        print(f"No rows in balance_history id range {lo}-{hi} — already cleaned?")
        return

    amounts = [r["amount"] for r in rows]
    if max(amounts) > 1000:
        print(f"WARN: range {lo}-{hi} contains amounts > $1000 "
              f"(max=${max(amounts):.2f}). NOT a phantom dip range. Aborting.")
        sys.exit(1)

    print(f"Deleting {len(rows)} phantom-dip rows "
          f"(${min(amounts):.2f}-${max(amounts):.2f}, "
          f"{rows[0]['recorded_at']} to {rows[-1]['recorded_at']})")

    cur.execute(
        "DELETE FROM balance_history WHERE id BETWEEN ? AND ?",
        (lo, hi),
    )
    print(f"Deleted rows: {cur.rowcount}")


def main():
    print(f"Repairing {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        repair_trade_39(con)
        print()
        clean_balance_history(con)
        con.commit()
        print("\nCommit OK.")
    except Exception:
        con.rollback()
        print("\nRolled back.")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
