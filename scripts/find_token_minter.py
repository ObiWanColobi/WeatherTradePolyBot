"""
Find which contract minted the winning tokens into the proxy.

Searches recent blocks on CTF for TransferSingle events where:
  to = proxy wallet
  id = stored token_id
The `from` address in the mint event is the adapter that created these
tokens. For Polymarket weather ladders, expect the NegRiskAdapter.

Also queries a candidate NegRiskAdapter to see if it recognizes the
conditionId.
"""
import sqlite3
import sys
from web3 import Web3

from config import WEATHER, WALLET_FUNDER_ADDRESS

_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

# Known Polymarket contracts on Polygon (public)
_CANDIDATE_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# TransferSingle(address indexed operator, address indexed from,
#                address indexed to, uint256 id, uint256 value)
_TRANSFER_SINGLE_TOPIC = Web3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()


def _topic_from_addr(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"cannot connect to {rpc}")
        sys.exit(1)

    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)
    proxy_topic = _topic_from_addr(proxy)

    latest = w3.eth.block_number
    # Search back ~2M blocks (~50 days on Polygon)
    # Ankr doesn't allow huge ranges; walk in chunks.
    chunk = 10_000

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, market_name, direction, token_id, market_id "
        "FROM trades WHERE token_id IS NOT NULL "
        "AND status IN ('claim_pending','open','closed') "
        "ORDER BY id DESC LIMIT 3"
    )
    rows = cur.fetchall()
    conn.close()

    # Probe NegRiskAdapter — try common view functions
    print(f"=== Probing candidate NegRiskAdapter at {_CANDIDATE_NEG_RISK_ADAPTER} ===")
    try:
        code = w3.eth.get_code(Web3.to_checksum_address(_CANDIDATE_NEG_RISK_ADAPTER))
        print(f"  has code: {len(code)} bytes")
    except Exception as e:
        print(f"  error: {e}")
    print()

    for tid, name, direction, token_id_str, cond_hex in rows:
        token_id = int(token_id_str)
        # Pad token_id to 32-byte topic
        id_topic = "0x" + hex(token_id)[2:].rjust(64, "0")

        print(f"#{tid} {direction} {name[:50]}")
        print(f"  searching for TransferSingle events minting token to proxy...")

        found = False
        # Walk back up to 500k blocks (~2 weeks on Polygon)
        for start in range(latest, max(latest - 500_000, 0), -chunk):
            end = start
            begin = max(start - chunk + 1, 0)
            try:
                logs = w3.eth.get_logs({
                    "fromBlock": begin,
                    "toBlock": end,
                    "address": _CTF,
                    "topics": [
                        _TRANSFER_SINGLE_TOPIC,
                        None,           # operator (any)
                        None,           # from (any)
                        proxy_topic,    # to = proxy
                    ],
                })
            except Exception as e:
                # RPC range limits — skip
                continue

            for log in logs:
                # data is [id, value] packed (64 bytes)
                data = log["data"]
                data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data) if isinstance(data, str) else data
                ev_id = int.from_bytes(data_bytes[:32], "big")
                ev_val = int.from_bytes(data_bytes[32:64], "big")
                if ev_id == token_id:
                    from_addr = "0x" + log["topics"][2].hex()[-40:]
                    operator = "0x" + log["topics"][1].hex()[-40:]
                    print(f"  FOUND @ block {log['blockNumber']}  tx={log['transactionHash'].hex()[:20]}...")
                    print(f"    operator (caller): {operator}")
                    print(f"    from:              {from_addr}")
                    print(f"    value:             {ev_val / 1e6:.4f}")
                    found = True
            if found:
                break

        if not found:
            print(f"  no mint event found in last 500k blocks")
        print()


if __name__ == "__main__":
    main()
