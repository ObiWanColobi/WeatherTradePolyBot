"""Use debug_traceCall on a tracing-capable public RPC to see the exact revert stack
for our failing NegRiskAdapter.redeemPositions eth_call.

Tries several RPCs in order; first one that accepts debug_traceCall wins.
"""
import json
import urllib.request

from web3 import Web3

from config import (
    WEATHER, POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)
from py_clob_client.client import ClobClient
import db


_FACTORY = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

_ADAPTER_ABI = [{"inputs":[{"name":"_conditionId","type":"bytes32"},
                            {"name":"amounts","type":"uint256[]"}],
                 "name":"redeemPositions","outputs":[],
                 "stateMutability":"nonpayable","type":"function"}]
_FACTORY_ABI = [{"inputs":[{"components":[
    {"name":"typeCode","type":"uint8"},{"name":"to","type":"address"},
    {"name":"value","type":"uint256"},{"name":"data","type":"bytes"}],
    "name":"calls","type":"tuple[]"}],"name":"proxy",
    "outputs":[{"name":"","type":"bytes[]"}],
    "stateMutability":"payable","type":"function"}]

TRACING_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon.blockpi.network/v1/rpc/public",
    "https://rpc.ankr.com/polygon",
]


def try_trace(rpc_url, from_addr, to_addr, data):
    """Returns (trace_dict_or_error, supported_bool)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "debug_traceCall",
        "params": [
            {"from": from_addr, "to": to_addr, "data": data},
            "latest",
            {"tracer": "callTracer", "tracerConfig": {"withLog": True}},
        ],
    }
    req = urllib.request.Request(
        rpc_url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        return {"transport_error": str(e)}, False
    if "error" in body:
        msg = body["error"].get("message", str(body["error"]))
        supported = "method not found" not in msg.lower() and "not supported" not in msg.lower()
        return body["error"], supported
    return body.get("result"), True


def walk(node, depth=0):
    """Pretty-print call tree with errors."""
    ind = "  " * depth
    typ = node.get("type", "?")
    to = node.get("to", "?")
    err = node.get("error") or node.get("revertReason")
    label = f"{ind}{typ} to={to}"
    if err:
        label += f"  !! {err}"
    print(label)
    # Show output decoded as utf-8 if it looks like a revert string
    out = node.get("output", "")
    if err and isinstance(out, str) and out.startswith("0x08c379a0"):
        # Error(string) revert — decode
        try:
            payload = bytes.fromhex(out[2 + 8 + 64:])  # skip selector + offset
            length = int.from_bytes(payload[:32], "big")
            msg = payload[32:32+length].decode("utf-8", errors="replace")
            print(f"{ind}   revert string: {msg!r}")
        except Exception:
            pass
    for child in node.get("calls", []) or []:
        walk(child, depth + 1)


def main():
    # Build the reverting outer calldata
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    eoa = w3.eth.account.from_key(WALLET_PRIVATE_KEY).address

    trade = db.get_trade_by_id(23)
    cond = trade["market_id"]
    kwargs = {"host": POLYMARKET_CLOB_API, "key": WALLET_PRIVATE_KEY,
              "chain_id": 137, "signature_type": WALLET_SIGNATURE_TYPE}
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    qid = ClobClient(**kwargs).get_market(cond)["question_id"]
    qid_bytes = bytes.fromhex(qid.replace("0x", ""))

    adapter = w3.eth.contract(address=_ADAPTER, abi=_ADAPTER_ABI)
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)

    # Use [0, 1] — smallest failing case; if this underflows we learn why
    amounts = [0, 1]
    inner_hex = adapter.functions.redeemPositions(qid_bytes, amounts)._encode_transaction_data()
    inner_bytes = bytes.fromhex(inner_hex[2:])
    call = (1, _ADAPTER, 0, inner_bytes)
    outer = factory.functions.proxy([call])._encode_transaction_data()

    print(f"from:  {eoa}")
    print(f"to:    {_FACTORY}")
    print(f"data:  {outer[:80]}...")
    print()

    for url in TRACING_RPCS:
        print(f"-- {url}")
        result, supported = try_trace(url, eoa, _FACTORY, outer)
        if not supported:
            print(f"   no debug_traceCall support: {result}")
            continue
        if isinstance(result, dict) and "error" in result:
            print(f"   RPC error: {result}")
            continue
        print(f"   OK — call tree:")
        walk(result)
        return

    print("\nNo tracing RPC worked. Try Alchemy/QuickNode with API key.")


if __name__ == "__main__":
    main()
