"""Scan recent blocks for PayoutRedemption events on the NegRiskAdapter.

If any exist with payout>0, pull the parent transaction and dump its input so we
can reverse-engineer the correct redemption shape (caller, calldata, target).
"""
from web3 import Web3

from config import WEATHER


_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
# keccak256("PayoutRedemption(address,bytes32,uint256[],uint256)")
_TOPIC = Web3.keccak(text="PayoutRedemption(address,bytes32,uint256[],uint256)").hex()


def scan(w3, from_block, to_block):
    try:
        logs = w3.eth.get_logs({
            "address": _ADAPTER,
            "topics": [_TOPIC],
            "fromBlock": from_block,
            "toBlock": to_block,
        })
        return logs
    except Exception as e:
        print(f"  [err] {e}")
        return []


def main():
    # Use public Polygon RPC for eth_getLogs (Ankr has tight range limits).
    rpc = "https://polygon-rpc.com"
    w3 = Web3(Web3.HTTPProvider(rpc))
    tip = w3.eth.block_number
    print(f"tip = {tip}")
    print(f"topic = 0x{_TOPIC}")

    window = 1000
    total_span = 200_000
    found = []
    start = tip - total_span
    end = tip
    while end > start and len(found) < 20:
        lo = max(start, end - window + 1)
        logs = scan(w3, lo, end)
        if logs:
            print(f"  blocks {lo}-{end}: {len(logs)} logs")
            found.extend(logs)
        end = lo - 1

    print(f"\nTotal PayoutRedemption logs: {len(found)}")

    # Decode a few with payout>0 and print the parent tx
    event_abi = {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "redeemer", "type": "address"},
            {"indexed": True, "name": "conditionId", "type": "bytes32"},
            {"indexed": False, "name": "amounts", "type": "uint256[]"},
            {"indexed": False, "name": "payout", "type": "uint256"},
        ],
        "name": "PayoutRedemption",
        "type": "event",
    }
    adapter = w3.eth.contract(address=_ADAPTER, abi=[event_abi])
    event = adapter.events.PayoutRedemption()

    shown = 0
    for log in found:
        try:
            parsed = event.process_log(log)
        except Exception as e:
            continue
        args = parsed["args"]
        payout = args["payout"]
        if payout == 0:
            continue
        tx_hash = log["transactionHash"].hex()
        tx = w3.eth.get_transaction(tx_hash)
        print()
        print(f"block       : {log['blockNumber']}")
        print(f"tx          : 0x{tx_hash}")
        print(f"tx.to       : {tx['to']}")
        print(f"tx.from     : {tx['from']}")
        print(f"redeemer    : {args['redeemer']}")
        print(f"conditionId : {args['conditionId'].hex() if isinstance(args['conditionId'], (bytes, bytearray)) else args['conditionId']}")
        print(f"amounts     : {args['amounts']}")
        print(f"payout      : {payout} ({payout/1e6:.4f} USDC)")
        print(f"input (first 200): {tx['input'].hex()[:200] if isinstance(tx['input'], (bytes, bytearray)) else tx['input'][:200]}")
        shown += 1
        if shown >= 3:
            break

    if shown == 0:
        print("\nNo PayoutRedemption events with payout>0 found in scanned range.")
        print("Either the adapter has no successful redemptions (architecture wrong?),")
        print("or our scan window missed them — try a wider range.")


if __name__ == "__main__":
    main()
