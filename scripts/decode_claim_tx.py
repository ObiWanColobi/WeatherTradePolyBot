"""
Decode the full PayoutRedemption event from our phantom claim txs.

The event is:
  PayoutRedemption(address indexed redeemer,
                   address indexed collateralToken,
                   bytes32 indexed parentCollectionId,
                   bytes32 conditionId,
                   uint256[] indexSets,
                   uint256 payout)

Topics 1-3 are indexed. topic[2] = collateralToken, topic[3] = parentCollectionId.
Our old Factory.proxy() calls produced payout=0. If we can see what
parentCollectionId and what indexSet the ProxyWallet forwarded, we can
figure out whether the wrong parameters were used.

Also queries the Polymarket CLOB API for market structure (negRisk flag,
token_ids, condition mapping).
"""
import json
import sqlite3
import sys
import urllib.request
from web3 import Web3

from config import WEATHER
from py_clob_client.client import ClobClient
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)

_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

_PAYOUT_TOPIC = "0x" + Web3.keccak(
    text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
).hex()


def decode_redemption_tx(w3, tx_hash):
    """Extract parentCollectionId, conditionId, indexSets, payout from a claim tx."""
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        return {"error": str(e)}
    if receipt is None:
        return {"error": "no receipt"}

    for log in receipt["logs"]:
        if log["address"].lower() != _CTF.lower():
            continue
        topics = log["topics"]
        if len(topics) < 4:
            continue
        topic0 = "0x" + topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
        if topic0.lower() != _PAYOUT_TOPIC.lower():
            continue

        collateral = "0x" + (topics[2].hex() if isinstance(topics[2], bytes) else topics[2])[-40:]
        parent_coll = "0x" + (topics[3].hex() if isinstance(topics[3], bytes) else topics[3])
        parent_coll = parent_coll[:66]

        data = log["data"]
        data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data) if isinstance(data, str) else data
        # Layout: conditionId(32) | offset_to_array(32) | payout(32) | array_len(32) | array_elems
        condition_id = "0x" + data_bytes[:32].hex()
        payout = int.from_bytes(data_bytes[64:96], "big")
        arr_len = int.from_bytes(data_bytes[96:128], "big")
        index_sets = []
        for i in range(arr_len):
            idx = int.from_bytes(data_bytes[128 + i*32:128 + (i+1)*32], "big")
            index_sets.append(idx)

        return {
            "collateral": collateral,
            "parentCollectionId": parent_coll,
            "conditionId": condition_id,
            "indexSets": index_sets,
            "payout": payout,
            "block": receipt["blockNumber"],
        }
    return {"error": "no PayoutRedemption in receipt"}


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))

    # ── 1. Decode our own redemption txs ──
    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, market_name, direction, market_id, token_id, claim_tx_hash "
        "FROM trades WHERE claim_tx_hash IS NOT NULL ORDER BY id DESC LIMIT 4"
    )
    rows = cur.fetchall()
    conn.close()

    print("=== Decoding phantom claim tx receipts ===\n")
    for tid, name, direction, cond, token_id, tx_hash in rows:
        print(f"#{tid} {direction} {name[:50]}")
        print(f"  stored cond:     {cond}")
        print(f"  stored token_id: {token_id}")
        print(f"  tx:              {tx_hash}")
        info = decode_redemption_tx(w3, tx_hash)
        for k, v in info.items():
            print(f"  {k}: {v}")
        if "conditionId" in info and info["conditionId"][2:].lower() != cond.replace("0x", "").lower():
            print(f"  !! conditionId in tx DIFFERS from stored conditionId")
        print()

    # ── 2. Ask CLOB API for market structure ──
    print("\n=== Querying Polymarket CLOB API for market metadata ===\n")
    kwargs = {
        "host": POLYMARKET_CLOB_API,
        "key": WALLET_PRIVATE_KEY,
        "chain_id": 137,
        "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    client = ClobClient(**kwargs)

    for tid, name, direction, cond, token_id, _ in rows[:3]:
        print(f"#{tid}  cond={cond}")
        try:
            mkt = client.get_market(cond)
            # Print key fields only
            keys_of_interest = [
                "condition_id", "question_id", "neg_risk", "neg_risk_market_id",
                "neg_risk_request_id", "tokens", "closed", "active",
                "end_date_iso", "outcomes",
            ]
            for k in keys_of_interest:
                if k in mkt:
                    print(f"  {k}: {mkt[k]}")
        except Exception as e:
            print(f"  CLOB error: {e}")
        print()


if __name__ == "__main__":
    main()
