"""
Verify the correct indexSet for each resolved trade.

The claim pipeline's phantom confirmations fire PayoutRedemption with
payout=0, meaning CTF.redeemPositions() is being invoked with parameters
that compute to a positionId the proxy wallet holds 0 of.

For each binary market, there are exactly two valid indexSets:
  indexSet=[1] -> outcome 0 (one direction)
  indexSet=[2] -> outcome 1 (the other)

Polymarket's mapping of YES/NO to outcome 0/1 is not guaranteed. This
script, for each resolved trade, computes:
  positionId_1 = getPositionId(USDC, getCollectionId(0, conditionId, 1))
  positionId_2 = getPositionId(USDC, getCollectionId(0, conditionId, 2))
and checks which matches the stored token_id, then reports the proxy's
on-chain balance at that positionId.

Usage:
    /opt/weatherbot/.venv/bin/python -m scripts.verify_index_sets
"""
import sqlite3
import sys
from web3 import Web3

from config import WEATHER, WALLET_FUNDER_ADDRESS

_USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_CTF  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

_CTF_ABI = [
    {"inputs": [{"type": "bytes32"}, {"type": "bytes32"}, {"type": "uint256"}],
     "name": "getCollectionId", "outputs": [{"type": "bytes32"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "bytes32"}],
     "name": "getPositionId", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}],
     "name": "balanceOf", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "payoutDenominator", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "uint256"}],
     "name": "payoutNumerators", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"cannot connect to {rpc}")
        sys.exit(1)

    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)
    print(f"Proxy wallet: {proxy}\n")

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT id, market_name, market_id, direction, token_id "
                "FROM trades WHERE market_id IS NOT NULL AND token_id IS NOT NULL")
    rows = cur.fetchall()
    conn.close()

    for tid, name, cond_hex, direction, token_id in rows:
        try:
            token_id_int = int(token_id)
        except Exception:
            print(f"#{tid}: bad token_id={token_id}")
            continue

        cond_bytes = bytes.fromhex(cond_hex.replace("0x", ""))
        zero32 = b"\x00" * 32

        try:
            coll_1 = ctf.functions.getCollectionId(zero32, cond_bytes, 1).call()
            coll_2 = ctf.functions.getCollectionId(zero32, cond_bytes, 2).call()
            pos_1 = ctf.functions.getPositionId(_USDC, coll_1).call()
            pos_2 = ctf.functions.getPositionId(_USDC, coll_2).call()
        except Exception as e:
            print(f"#{tid}: RPC error: {e}")
            continue

        match = None
        if pos_1 == token_id_int:
            match = 1
        elif pos_2 == token_id_int:
            match = 2

        # Check on-chain state
        try:
            denom = ctf.functions.payoutDenominator(cond_bytes).call()
            num0 = ctf.functions.payoutNumerators(cond_bytes, 0).call() if denom > 0 else None
            num1 = ctf.functions.payoutNumerators(cond_bytes, 1).call() if denom > 0 else None
            bal_1 = ctf.functions.balanceOf(proxy, pos_1).call()
            bal_2 = ctf.functions.balanceOf(proxy, pos_2).call()
        except Exception as e:
            print(f"#{tid}: state RPC error: {e}")
            continue

        old_code_indexset = 1 if direction == "YES" else 2

        print(f"#{tid} {direction:3s} {name[:55]}")
        print(f"  token_id matches indexSet=[{match}]  "
              f"(old code would pass [{old_code_indexset}])")
        print(f"  proxy balance @ pos_1 (indexSet=[1]): {bal_1/1e6:.4f}")
        print(f"  proxy balance @ pos_2 (indexSet=[2]): {bal_2/1e6:.4f}")
        if denom > 0:
            print(f"  oracle: denom={denom}  numerators=[{num0}, {num1}]  "
                  f"(winning slot = {0 if num0 else 1})")
        else:
            print(f"  oracle: NOT RESOLVED YET")
        print()


if __name__ == "__main__":
    main()
