"""Probe NegRiskAdapter's internal view of our qid, then test bypass via direct CTF.

Layer A: does the adapter know this qid is resolved?
  - getDetermined(qid), getPayout(qid), getMetadata(qid)
  - If getPayout reverts, adapter has no result for this qid → redeemPositions must underflow.

Layer B: bypass — call CTF.redeemPositions(wcol, 0x0, cond, [1,2]) directly from proxy.
  - This is what NegRiskAdapter does internally (step 2).
  - Returns WCOL to caller. We'd then call wcol.unwrap(proxy, amount) to get USDC.
  - eth_call only — no signing, no tx broadcast.
"""
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
_WCOL = Web3.to_checksum_address("0x3A3BD7bb9528E159577F7C2e685CC81A765002E2")

_ADAPTER_ABI = [
    {"inputs":[{"name":"q","type":"bytes32"}],"name":"getDetermined",
     "outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"q","type":"bytes32"}],"name":"getPayout",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"q","type":"bytes32"}],"name":"getMetadata",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"q","type":"bytes32"}],"name":"getResult",
     "outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"q","type":"bytes32"}],"name":"getOracle",
     "outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
]

_CTF_ABI = [
    {"inputs":[
        {"name":"collateralToken","type":"address"},
        {"name":"parentCollectionId","type":"bytes32"},
        {"name":"conditionId","type":"bytes32"},
        {"name":"indexSets","type":"uint256[]"},
     ], "name":"redeemPositions","outputs":[],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"a","type":"address"},{"name":"id","type":"uint256"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]

_WCOL_ABI = [
    {"inputs":[],"name":"underlying","outputs":[{"name":"","type":"address"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

_FACTORY_ABI = [{"inputs":[{"components":[
    {"name":"typeCode","type":"uint8"},{"name":"to","type":"address"},
    {"name":"value","type":"uint256"},{"name":"data","type":"bytes"}],
    "name":"calls","type":"tuple[]"}],"name":"proxy",
    "outputs":[{"name":"","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]


def safe_call(fn, label):
    try:
        v = fn.call()
        print(f"  {label}: {v}")
        return v
    except Exception as e:
        print(f"  {label}: REVERT ({str(e)[:120]})")
        return None


def try_eth_call(w3, frm, to, data, label):
    try:
        w3.eth.call({"from": frm, "to": to, "data": data})
        print(f"  [{label}] OK — no revert")
        return True
    except Exception as e:
        print(f"  [{label}] REVERT: {str(e)[:200]}")
        return False


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
    cond_bytes = bytes.fromhex(cond.replace("0x", ""))

    adapter = w3.eth.contract(address=_ADAPTER, abi=_ADAPTER_ABI)
    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)
    wcol = w3.eth.contract(address=_WCOL, abi=_WCOL_ABI)
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)

    print(f"EOA:     {eoa}")
    print(f"Proxy:   {proxy}")
    print(f"qid:     {qid}")
    print(f"cond:    {cond}")
    print()

    print("== Layer A: adapter's view of this qid ==")
    safe_call(adapter.functions.getDetermined(qid_bytes),  "getDetermined  ")
    safe_call(adapter.functions.getResult(qid_bytes),      "getResult      ")
    safe_call(adapter.functions.getPayout(qid_bytes),      "getPayout      ")
    safe_call(adapter.functions.getMetadata(qid_bytes),    "getMetadata    ")
    safe_call(adapter.functions.getOracle(qid_bytes),      "getOracle      ")
    print()

    print("== WCOL sanity ==")
    underlying = safe_call(wcol.functions.underlying(), "wcol.underlying")
    safe_call(wcol.functions.balanceOf(proxy),       "wcol balance(proxy)")
    safe_call(wcol.functions.balanceOf(_ADAPTER),    "wcol balance(adapter)")
    print()

    print("== Layer B: bypass — direct CTF.redeemPositions(wcol, 0x0, cond, [1,2]) ==")
    # This is what the NegRiskAdapter does internally. Proxy holds wcol-CTF positions
    # at token IDs Helpers.positionIds(wcol, cond)[0 and 1]. If CTF is aware of wcol
    # as collateral for this cond, this should succeed with from=proxy.
    index_sets = [1, 2]
    redeem_data = ctf.functions.redeemPositions(
        _WCOL, b"\x00"*32, cond_bytes, index_sets
    )._encode_transaction_data()

    try_eth_call(w3, proxy, _CTF, redeem_data, "B1. Proxy -> CTF.redeemPositions(wcol,...)")
    try_eth_call(w3, eoa,   _CTF, redeem_data, "B2. EOA   -> CTF.redeemPositions(wcol,...) (should underflow — EOA has 0 balance)")

    # Wrapped via Factory (the actual bot path we'd use if B1 succeeds)
    inner = bytes.fromhex(redeem_data[2:])
    call = (1, _CTF, 0, inner)
    outer = factory.functions.proxy([call])._encode_transaction_data()
    try_eth_call(w3, eoa, _FACTORY, outer, "B3. EOA -> Factory -> CTF.redeemPositions(wcol,...)  (prod path)")

    print()
    print("== Layer B variants — try just [1] or [2] (single index set) ==")
    for iset in ([1], [2], [1, 2]):
        data = ctf.functions.redeemPositions(_WCOL, b"\x00"*32, cond_bytes, iset)._encode_transaction_data()
        try_eth_call(w3, proxy, _CTF, data, f"proxy -> CTF.redeemPositions wcol indexSets={iset}")


if __name__ == "__main__":
    main()
