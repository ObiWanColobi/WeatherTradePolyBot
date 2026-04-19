"""
Inspect a claim tx on-chain to verify it actually redeemed USDC.

A tx receipt with status=1 only means the outer Factory.proxy() call
didn't revert. It does NOT mean the inner CTF.redeemPositions() actually
moved any USDC. If the Factory derived the wrong proxy wallet, or the
inner call was a no-op (0 tokens held), the outer tx still succeeds.

This script reads a tx receipt and checks:
  1. Was there a PayoutRedemption event (signals CTF actually redeemed)?
  2. Was there a USDC Transfer event (signals money moved)?
  3. Where did the USDC land?

Usage:
    python3 -m scripts.inspect_claim_tx <tx_hash>
    python3 -m scripts.inspect_claim_tx                # inspects all DB tx hashes
"""
import os
import sqlite3
import sys
from web3 import Web3

from config import WEATHER

# PayoutRedemption(address indexed redeemer, address indexed collateralToken,
#                  bytes32 indexed parentCollectionId, bytes32 conditionId,
#                  uint256[] indexSets, uint256 payout)
_PAYOUT_REDEMPTION_TOPIC = Web3.keccak(
    text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
).hex()

# Transfer(address indexed from, address indexed to, uint256 value)
_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

_USDC = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
_CTF  = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"


def _h(x):
    if isinstance(x, bytes):
        x = "0x" + x.hex()
    return x.lower() if x.startswith("0x") else "0x" + x.lower()


def inspect(w3, tx_hash):
    tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
    print(f"\n=== Tx {tx_hash[:20]}... ===")
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        print(f"  receipt error: {e}")
        return
    if receipt is None:
        print("  no receipt (pending or dropped)")
        return

    print(f"  status={receipt['status']}  block={receipt['blockNumber']}  gasUsed={receipt['gasUsed']}")
    print(f"  to: {receipt['to']}")
    print(f"  logs: {len(receipt['logs'])} event(s)")

    payout_events = 0
    usdc_transfers = []
    for log in receipt["logs"]:
        topic0 = _h(log["topics"][0]) if log["topics"] else ""
        addr = _h(log["address"])
        if topic0 == _h(_PAYOUT_REDEMPTION_TOPIC) and addr == _CTF:
            payout_events += 1
            redeemer = "0x" + log["topics"][1].hex()[-40:]
            # payout is at end of data (last 32 bytes)
            data = log["data"]
            data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data) if isinstance(data, str) else data
            payout_raw = int.from_bytes(data_bytes[-32:], "big")
            print(f"  [PayoutRedemption] redeemer={redeemer}  payout={payout_raw / 1e6:.4f} USDC")
        elif topic0 == _h(_TRANSFER_TOPIC) and addr == _USDC:
            from_addr = "0x" + log["topics"][1].hex()[-40:]
            to_addr = "0x" + log["topics"][2].hex()[-40:]
            data = log["data"]
            data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data) if isinstance(data, str) else data
            val = int.from_bytes(data_bytes, "big") / 1e6
            usdc_transfers.append((from_addr, to_addr, val))
            print(f"  [USDC Transfer] {from_addr[:10]}... -> {to_addr[:10]}...  {val:.4f} USDC")

    if payout_events == 0:
        print("  >> NO PayoutRedemption event — CTF.redeemPositions was NOT actually called")
    if not usdc_transfers:
        print("  >> NO USDC movement — this was a phantom confirmation")


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"cannot connect to {rpc}")
        sys.exit(1)

    if len(sys.argv) > 1:
        inspect(w3, sys.argv[1])
        return

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT id, market_name, claim_tx_hash FROM trades "
                "WHERE claim_tx_hash IS NOT NULL")
    rows = cur.fetchall()
    conn.close()
    for tid, name, tx in rows:
        print(f"\n--- Trade #{tid}: {name[:60]} ---")
        inspect(w3, tx)


if __name__ == "__main__":
    main()
