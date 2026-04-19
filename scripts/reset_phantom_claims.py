"""
Reset phantom-confirmed claims.

Rows #16, 17, 18, 20, 21 were marked `claim_confirmed` but no USDC
was ever received — they were executed against the old claimer that
called CTF.redeemPositions() with USDC as collateral, which the proxy
does NOT hold (proxy holds wcol-collateralized positions under the
NegRiskAdapter flow). Their tx hashes returned status=1 with payout=0.

Row #23 is NOT included — it was hand-fixed to reflect the live Phase 2
redemption (tx 0x3ee55f89…, +$23.5358 USDC actual). Leave it as
claim_confirmed.

This script resets the remaining phantoms back to `claim_pending` so the
new bundled CTF+wcol claimer can retry them against the actual proxy.

Usage:
    python -m scripts.reset_phantom_claims --dry-run
    python -m scripts.reset_phantom_claims
"""
import sqlite3
import sys

PHANTOM_IDS = [16, 17, 18, 20, 21]
DB_PATH = "weather_bot.db"


def main():
    dry_run = "--dry-run" in sys.argv

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    placeholders = ",".join("?" * len(PHANTOM_IDS))
    cur.execute(
        f"SELECT id, market_name, status, claim_status, claim_tx_hash "
        f"FROM trades WHERE id IN ({placeholders})",
        PHANTOM_IDS,
    )
    rows = cur.fetchall()

    print(f"{'DRY RUN — ' if dry_run else ''}Before reset:")
    for r in rows:
        tx = (r[4][:16] + "...") if r[4] else None
        print(f"  #{r[0]}: status={r[2]} claim_status={r[3]} tx={tx}")

    if dry_run:
        print("\n(dry run — no changes written)")
        conn.close()
        return

    cur.execute(
        f"UPDATE trades SET "
        f"status='claim_pending', "
        f"claim_status='claim_pending', "
        f"claim_tx_hash=NULL, "
        f"claim_retries=0, "
        f"claim_last_attempt=NULL, "
        f"closed_at=NULL "
        f"WHERE id IN ({placeholders})",
        PHANTOM_IDS,
    )
    conn.commit()

    print(f"\nReset {cur.rowcount} rows.")
    print("Claimer will retry these on the next bot cycle.")
    conn.close()


if __name__ == "__main__":
    main()
