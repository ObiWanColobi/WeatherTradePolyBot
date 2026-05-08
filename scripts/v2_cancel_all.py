"""
One-off: cancel every open order on the bot's CLOB V2 account.

Use after a stuck-order incident (e.g. when _cancel_order failed and the bot
can't tell whether an order is still on the book). The bot's startup already
calls cancel_all(), but if the network blipped at startup or the bot is
stopped, run this manually.

Usage:
    python scripts/v2_cancel_all.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from py_clob_client_v2.client import ClobClient

from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)

CHAIN_ID = 137


def main():
    if not WALLET_PRIVATE_KEY:
        print("ERROR: WALLET_PRIVATE_KEY not set"); sys.exit(1)

    kwargs = {
        "host": POLYMARKET_CLOB_API,
        "key": WALLET_PRIVATE_KEY,
        "chain_id": CHAIN_ID,
        "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    client = ClobClient(**kwargs)
    client.set_api_creds(client.create_or_derive_api_key())
    print("CLOB V2 client initialized.")

    result = client.cancel_all()
    print(f"cancel_all() raw response: {result}")

    if not result:
        print("No open orders.")
        return

    cancelled = result if isinstance(result, list) else result.get("canceled", [])
    not_cancelled = result.get("not_canceled", {}) if isinstance(result, dict) else {}
    if cancelled:
        print(f"Cancelled {len(cancelled)} order(s):")
        for oid in cancelled:
            print(f"  {oid}")
    if not_cancelled:
        print(f"Failed to cancel {len(not_cancelled)} order(s):")
        for oid, reason in not_cancelled.items():
            print(f"  {oid}: {reason}")


if __name__ == "__main__":
    main()
