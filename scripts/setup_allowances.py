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
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    print(f"CLOB API authenticated successfully")

    # Check and update COLLATERAL (pUSD post-V2) allowance.
    # V2 may return an "allowances" dict keyed by exchange contract address
    # rather than a single "allowance" string — handle both shapes.
    print("\nChecking COLLATERAL (pUSD) allowance...")
    bal_info = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    balance = bal_info.get("balance", "0")

    def _has_allowance(info: dict) -> bool:
        allowances = info.get("allowances", {})
        if allowances:
            return any(int(v) > 0 for v in allowances.values())
        return int(info.get("allowance", "0") or 0) > 0

    print(f"  Balance: {int(balance) / 1e6:.2f} pUSD")
    print(f"  Raw response: {bal_info}")

    if not _has_allowance(bal_info):
        print("  Updating COLLATERAL allowance via CLOB API (no gas needed)...")
        resp = client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Response: {resp}")

        # Verify
        bal_info = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        print(f"  Post-update response: {bal_info}")
    else:
        print("  Already approved — skipping.")

    ok = _has_allowance(bal_info)
    print(f"\nSetup complete.")
    print(f"  pUSD balance: {int(balance) / 1e6:.2f}")
    print(f"  COLLATERAL allowance: {'OK' if ok else 'FAILED'}")
    print(f"\nNote: CONDITIONAL (CTF token) allowances are per-token and set")
    print(f"automatically when you trade. No manual setup needed.")
    print(f"\nIf you have lingering USDC.e from pre-V2 claims, wrap it to pUSD:")
    print(f"  python scripts/wrap_usdce_to_pusd.py")
    print(f"\nYou can now run the bot in live mode.")
    print(f"  Set TRADING_MODE=live in your .env file to enable.")


if __name__ == "__main__":
    main()
