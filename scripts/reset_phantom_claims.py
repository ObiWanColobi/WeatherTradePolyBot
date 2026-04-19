"""
Reset phantom-confirmed claims.

Rows #16, 17, 18, 20, 21, 23 were marked `claim_confirmed` but no USDC
was ever received — they were executed against the old claimer that
called CTF.redeemPositions() directly from the EOA (which owns nothing
under POLY_PROXY / SignatureType=1). Their tx hashes returned status=1
with payout=0.

This script resets them back to `claim_pending` so the new Factory-
routed claimer can retry them against the actual proxy wallet.

Run ONCE on the VPS after deploying the claimer fix:
    python -m scripts.reset_phantom_claims
"""
import sqlite3

PHANTOM_IDS = [16, 17, 18, 20, 21, 23]
DB_PATH = "weather_bot.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    placeholders = ",".join("?" * len(PHANTOM_IDS))
    cur.execute(
        f"SELECT id, market_name, status, claim_status, claim_tx_hash "
        f"FROM trades WHERE id IN ({placeholders})",
        PHANTOM_IDS,
    )
    rows = cur.fetchall()

    print("Before reset:")
    for r in rows:
        print(f"  #{r[0]}: status={r[2]} claim_status={r[3]} tx={r[4][:16] if r[4] else None}...")

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
