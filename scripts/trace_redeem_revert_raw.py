"""Dump raw debug_traceCall response from Ankr to see what we actually got."""
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


def call_rpc(url, method, params):
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
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

    amounts = [0, 1]
    inner_hex = adapter.functions.redeemPositions(qid_bytes, amounts)._encode_transaction_data()
    inner_bytes = bytes.fromhex(inner_hex[2:])
    call = (1, _ADAPTER, 0, inner_bytes)
    outer = factory.functions.proxy([call])._encode_transaction_data()

    ankr = "https://rpc.ankr.com/polygon"

    print("=== plain eth_call (baseline) ===")
    resp = call_rpc(ankr, "eth_call", [
        {"from": eoa, "to": _FACTORY, "data": outer}, "latest"
    ])
    print(json.dumps(resp, indent=2)[:2000])

    print("\n=== debug_traceCall with callTracer ===")
    resp = call_rpc(ankr, "debug_traceCall", [
        {"from": eoa, "to": _FACTORY, "data": outer},
        "latest",
        {"tracer": "callTracer", "tracerConfig": {"withLog": True}},
    ])
    print(json.dumps(resp, indent=2)[:8000])

    print("\n=== debug_traceCall default tracer (opcode-level, truncated) ===")
    resp = call_rpc(ankr, "debug_traceCall", [
        {"from": eoa, "to": _FACTORY, "data": outer},
        "latest",
        {},
    ])
    # Default tracer returns massive structLog — just show top-level fields
    if "result" in resp:
        r = resp["result"]
        if isinstance(r, dict):
            print(f"keys: {list(r.keys())}")
            print(f"failed: {r.get('failed')}")
            print(f"gas: {r.get('gas')}")
            ret = r.get("returnValue", "")
            print(f"returnValue: {ret[:200]}")
            sl = r.get("structLog") or r.get("structLogs") or []
            print(f"structLogs: {len(sl)} entries")
            # Show last ~10 opcodes — that's where the revert happens
            for entry in sl[-15:]:
                print(f"  pc={entry.get('pc')} op={entry.get('op')} depth={entry.get('depth')} gas={entry.get('gas')}")
    else:
        print(json.dumps(resp, indent=2)[:2000])


if __name__ == "__main__":
    main()
