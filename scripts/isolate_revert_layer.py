"""Isolate WHICH layer of the call stack reverts.

Three eth_call tests:
  1. from=EOA, to=Adapter, redeemPositions([0,1])    — EOA calls adapter directly
  2. from=Proxy, to=Adapter, redeemPositions([0,1])  — Proxy calls adapter directly (what Factory.proxy does internally)
  3. from=EOA, to=Factory, proxy([...])              — full wrapped path (known to revert)

If #2 reverts but #1 doesn't: the proxy can't redeem (likely because EOA holds tokens, not proxy — but we verified proxy DOES hold them, so would indicate adapter-level check on caller identity).
If #2 succeeds but #3 reverts: issue is in the Factory/ProxyWallet layer itself.
If #1 reverts: issue is even more fundamental (wrong qid? wrong amounts shape?).
"""
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


def try_call(w3, frm, to, data, label):
    try:
        w3.eth.call({"from": frm, "to": to, "data": data})
        print(f"  [{label}] OK — no revert")
    except Exception as e:
        msg = str(e)
        # Try to pull structured revert data
        short = msg[:300]
        print(f"  [{label}] REVERT: {short}")


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    eoa = w3.eth.account.from_key(WALLET_PRIVATE_KEY).address
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

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

    print(f"EOA:     {eoa}")
    print(f"Proxy:   {proxy}")
    print(f"Adapter: {_ADAPTER}")
    print(f"Factory: {_FACTORY}")
    print(f"qid:     {qid}")
    print(f"amounts: {amounts}")
    print()

    print("== eth_call layer-by-layer ==")
    # 1. EOA -> Adapter directly (EOA has no tokens, so this SHOULD revert on balance,
    #    but amounts=[0,1] means only 1 wei of NO token needed — EOA has 0, so expect underflow)
    try_call(w3, eoa, _ADAPTER, inner_hex, "1. EOA -> Adapter  (direct)")

    # 2. Proxy -> Adapter directly (proxy has 23.5M NO tokens, approval=True)
    try_call(w3, proxy, _ADAPTER, inner_hex, "2. Proxy -> Adapter (direct)")

    # 3. EOA -> Factory -> (proxy internally) -> Adapter (the actual bot path)
    try_call(w3, eoa, _FACTORY, outer, "3. EOA -> Factory -> Adapter (wrapped)")


if __name__ == "__main__":
    main()
