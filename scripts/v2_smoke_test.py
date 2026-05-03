"""
V2 smoke test — minimum-cost end-to-end verification of the CLOB V2 port.

Steps:
1. Init V2 ClobClient with the same kwargs the bot uses.
2. Read pUSD balance + allowance, print raw response.
3. Optionally place a tiny BUY (default $1) on a token_id you specify, then
   immediately attempt to close the position with a SELL at best bid.

Usage:
    # dry-run: just init + read balance/allowance
    python scripts/v2_smoke_test.py

    # live test: $1 BUY at the given price, then unwind
    python scripts/v2_smoke_test.py <token_id> <buy_price>

The script intentionally avoids the LiveExecutor wrapper so a successful run
proves the SDK + creds + allowance + signing path are healthy in isolation.
Pick a high-liquidity neg-risk weather market for the live test so the FOK
fills cleanly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    AssetType, BalanceAllowanceParams, MarketOrderArgs, OrderArgs, OrderType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)

CHAIN_ID = 137


def init_client() -> ClobClient:
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
    return client


def read_balance(client: ClobClient) -> float:
    bal_info = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    print("Balance/allowance raw response:")
    print(f"  {bal_info}")
    raw = bal_info.get("balance", "0")
    return int(raw) / 1_000_000


def main():
    if not WALLET_PRIVATE_KEY:
        print("ERROR: WALLET_PRIVATE_KEY not set"); sys.exit(1)

    print(f"Host:           {POLYMARKET_CLOB_API}")
    print(f"Signature type: {WALLET_SIGNATURE_TYPE}")
    print(f"Funder (proxy): {WALLET_FUNDER_ADDRESS}")
    print()

    client = init_client()
    print("CLOB V2 client initialized + creds set.\n")

    balance = read_balance(client)
    print(f"\nResolved pUSD balance: ${balance:.2f}\n")

    if len(sys.argv) < 3:
        print("Dry run complete. To smoke a live order:")
        print("  python scripts/v2_smoke_test.py <token_id> <buy_price>")
        return

    token_id = sys.argv[1]
    buy_price = float(sys.argv[2])
    print(f"=== LIVE SMOKE TEST ===")
    print(f"  token_id: {token_id}")
    print(f"  price:    {buy_price}")
    print(f"  amount:   $1.00 pUSD")
    confirm = input("Type YES to proceed: ").strip()
    if confirm != "YES":
        print("Aborted."); return

    buy_args = MarketOrderArgs(
        token_id=token_id,
        price=buy_price,
        amount=1.00,
        side=BUY,
        user_usdc_balance=float(balance),
    )
    print("\nSigning BUY...")
    signed_buy = client.create_market_order(buy_args)
    print("Posting BUY (FOK)...")
    buy_resp = client.post_order(signed_buy, OrderType.FOK)
    print(f"BUY response: {buy_resp}")

    status = (buy_resp or {}).get("status", "")
    if "matched" not in str(status).lower() and "filled" not in str(status).lower():
        print("\nBUY did not match (FOK) — nothing to unwind. Exiting.")
        return

    matched_size = float(buy_resp.get("size_matched", 0) or 0)
    if matched_size <= 0:
        print("\nBUY response had no size_matched — exiting without unwind.")
        return

    print(f"\nBUY filled {matched_size} shares. Unwinding via SELL...")
    sell_price = max(0.01, round(buy_price - 0.02, 2))
    sell_args = OrderArgs(
        token_id=token_id,
        price=sell_price,
        size=matched_size,
        side=SELL,
    )
    signed_sell = client.create_order(sell_args)
    sell_resp = client.post_order(signed_sell, OrderType.GTC)
    print(f"SELL response: {sell_resp}")
    print("\nIf the SELL didn't fill, manually cancel + close the position via the Polymarket UI.")


if __name__ == "__main__":
    main()
