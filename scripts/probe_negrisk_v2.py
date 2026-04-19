"""
Re-probe NegRiskAdapter using questionId (its internal marketId) instead
of the CTF conditionId. The adapter derives CTF conditionIds from its
own marketId via keccak(adapter, questionId, 2), so its public getters
must be queried with questionId.
"""
import sqlite3
import sys
from web3 import Web3

from config import WEATHER
from py_clob_client.client import ClobClient
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)

_NR = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

# NegRiskAdapter ABI — key public methods
_NR_ABI = [
    {"inputs": [{"type": "bytes32"}, {"type": "bool"}],
     "name": "getPositionId", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getDetermined", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getPayout", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getConditionId", "outputs": [{"type": "bytes32"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}],
     "name": "getOracle", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
]


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    adapter = w3.eth.contract(address=_NR, abi=_NR_ABI)

    kwargs = {
        "host": POLYMARKET_CLOB_API,
        "key": WALLET_PRIVATE_KEY,
        "chain_id": 137,
        "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    client = ClobClient(**kwargs)

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, market_name, direction, market_id, token_id "
        "FROM trades WHERE market_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 4"
    )
    rows = cur.fetchall()
    conn.close()

    for tid, name, direction, cond, token_id_str in rows:
        token_id = int(token_id_str)
        print(f"#{tid} {direction} {name[:55]}")
        try:
            mkt = client.get_market(cond)
        except Exception as e:
            print(f"  CLOB error: {e}\n")
            continue
        qid = mkt.get("question_id")
        qbytes = bytes.fromhex(qid.replace("0x", ""))

        print(f"  questionId:  {qid}")
        print(f"  token_id:    {token_id_str}")

        for fn_name, args in [
            ("getDetermined", [qbytes]),
            ("getPayout",     [qbytes]),
            ("getConditionId",[qbytes]),
            ("getOracle",     [qbytes]),
            ("getPositionId", [qbytes, True]),
            ("getPositionId", [qbytes, False]),
        ]:
            try:
                result = getattr(adapter.functions, fn_name)(*args).call()
                label = f"{fn_name}({','.join([a.hex() if isinstance(a, bytes) else str(a) for a in args])})"
                label = label[:70]
                print(f"  {label} = {result}")
                if fn_name == "getPositionId":
                    is_yes = args[1]
                    if result == token_id:
                        print(f"    >> MATCH: this token is the {'YES' if is_yes else 'NO'} outcome")
            except Exception as e:
                print(f"  {fn_name} failed: {e}")
        print()


if __name__ == "__main__":
    main()
