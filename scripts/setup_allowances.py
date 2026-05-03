"""
One-time USDC + CTF allowance setup for Polymarket CLOB trading.

Uses the CLOB API's update_balance_allowance() endpoint — no gas or MATIC needed.

Usage:
    python scripts/setup_allowances.py

Requires:
    - WALLET_PRIVATE_KEY set in .env
    - WALLET_SIGNATURE_TYPE set in .env (1 for Magic Link / POLY_PROXY)
    - WALLET_FUNDER_ADDRESS set in .env (your Polymarket proxy address)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config import (
    WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE,
    WALLET_FUNDER_ADDRESS,
    POLYMARKET_CLOB_API,
)

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

CHAIN_ID = 137


def main():
    if not WALLET_PRIVATE_KEY:
        print("ERROR: WALLET_PRIVATE_KEY not set in .env")
        sys.exit(1)

    print(f"Signature type: {WALLET_SIGNATURE_TYPE} "
          f"({'EOA' if WALLET_SIGNATURE_TYPE == 0 else 'POLY_PROXY' if WALLET_SIGNATURE_TYPE == 1 else 'GNOSIS_SAFE'})")
    if WALLET_FUNDER_ADDRESS:
        print(f"Proxy address:  {WALLET_FUNDER_ADDRESS}")

    # Initialize CLOB client
    kwargs = {
        "host": POLYMARKET_CLOB_API,
        "key": WALLET_PRIVATE_KEY,
        "chain_id": CHAIN_ID,
        "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS

    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print(f"CLOB API authenticated successfully")

    # Check and update COLLATERAL (USDC) allowance
    print("\nChecking COLLATERAL (USDC) allowance...")
    bal_info = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    balance = bal_info.get("balance", "0")
    allowance = bal_info.get("allowance", "0")
    print(f"  Balance: {int(balance) / 1e6:.2f} USDC, Allowance: {allowance}")

    if allowance == "0" or allowance == 0:
        print("  Updating COLLATERAL allowance via CLOB API (no gas needed)...")
        resp = client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Response: {resp}")

        # Verify
        bal_info = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        allowance = bal_info.get("allowance", "0")
        print(f"  Updated allowance: {allowance}")
    else:
        print("  Already approved — skipping.")

    print(f"\nSetup complete.")
    print(f"  USDC balance: {int(balance) / 1e6:.2f}")
    print(f"  COLLATERAL allowance: {'OK' if allowance not in ('0', 0) else 'FAILED'}")
    print(f"\nNote: CONDITIONAL (CTF token) allowances are per-token and set")
    print(f"automatically when you trade. No manual setup needed.")
    print(f"\nYou can now run the bot in live mode.")
    print(f"  Set TRADING_MODE=live in your .env file to enable.")


if __name__ == "__main__":
    main()
