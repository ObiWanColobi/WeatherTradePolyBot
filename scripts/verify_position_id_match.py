"""Cross-check: compute standard CTF position IDs via CTF.getCollectionId + CTF.getPositionId
and compare with NegRiskAdapter.getPositionId and our stored token_id.

If they disagree, we've found the bug: proxy holds tokens at some ID scheme that the adapter's
redeemPositions (which uses Helpers.positionIds(wcol, CLOB_conditionId)) cannot touch.
"""
from web3 import Web3

from config import (
    WEATHER, POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)
from py_clob_client.client import ClobClient
import db


_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
_WCOL = Web3.to_checksum_address("0x3A3BD7bb9528E159577F7C2e685CC81A765002E2")
_USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

_CTF_ABI = [
    {"inputs":[{"name":"parent","type":"bytes32"},{"name":"cond","type":"bytes32"},{"name":"indexSet","type":"uint256"}],
     "name":"getCollectionId","outputs":[{"name":"","type":"bytes32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"collateralToken","type":"address"},{"name":"collectionId","type":"bytes32"}],
     "name":"getPositionId","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"a","type":"address"},{"name":"id","type":"uint256"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"cond","type":"bytes32"}],
     "name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"cond","type":"bytes32"},{"name":"i","type":"uint256"}],
     "name":"payoutNumerators","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"cond","type":"bytes32"}],
     "name":"getOutcomeSlotCount","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]
_ADAPTER_ABI = [
    {"inputs":[{"name":"q","type":"bytes32"}],
     "name":"getConditionId","outputs":[{"name":"","type":"bytes32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"q","type":"bytes32"},{"name":"b","type":"bool"}],
     "name":"getPositionId","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    trade = db.get_trade_by_id(23)
    cond = trade["market_id"]
    stored_token_id = int(trade["token_id"])

    kwargs = {"host": POLYMARKET_CLOB_API, "key": WALLET_PRIVATE_KEY,
              "chain_id": 137, "signature_type": WALLET_SIGNATURE_TYPE}
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    qid = ClobClient(**kwargs).get_market(cond)["question_id"]
    qid_bytes = bytes.fromhex(qid.replace("0x", ""))
    cond_bytes = bytes.fromhex(cond.replace("0x", ""))

    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)
    adapter = w3.eth.contract(address=_ADAPTER, abi=_ADAPTER_ABI)

    # 1. Verify CLOB conditionId == adapter.getConditionId(qid)
    adapter_cond = adapter.functions.getConditionId(qid_bytes).call()
    adapter_cond_hex = "0x" + adapter_cond.hex()
    print(f"CLOB conditionId:    {cond}")
    print(f"adapter.getConditionId(qid): {adapter_cond_hex}")
    print(f"match: {cond.lower() == adapter_cond_hex.lower()}")

    # 2. Compute standard CTF position ids for wcol + cond + indexSet [1, 2]
    print("\n-- wcol-collateralized CTF positions --")
    for ix in (1, 2):
        coll_id = ctf.functions.getCollectionId(b"\x00"*32, cond_bytes, ix).call()
        pos_id = ctf.functions.getPositionId(_WCOL, coll_id).call()
        bal = ctf.functions.balanceOf(proxy, pos_id).call()
        side = "YES (indexSet=1)" if ix == 1 else "NO  (indexSet=2)"
        print(f"  {side}")
        print(f"    collectionId = 0x{coll_id.hex()}")
        print(f"    positionId   = {pos_id}")
        print(f"    proxy balance= {bal}  ({bal/1e6:.4f})")

    # 3. Same computation but with USDC as collateral (the old, broken path)
    print("\n-- USDC-collateralized CTF positions (old phantom path) --")
    for ix in (1, 2):
        coll_id = ctf.functions.getCollectionId(b"\x00"*32, cond_bytes, ix).call()
        pos_id = ctf.functions.getPositionId(_USDC, coll_id).call()
        bal = ctf.functions.balanceOf(proxy, pos_id).call()
        side = "YES" if ix == 1 else "NO "
        print(f"  {side}: positionId={pos_id}  balance={bal}")

    # 4. adapter.getPositionId for comparison
    print("\n-- adapter.getPositionId --")
    for outcome in (True, False):
        pid = adapter.functions.getPositionId(qid_bytes, outcome).call()
        bal = ctf.functions.balanceOf(proxy, pid).call()
        print(f"  getPositionId(qid, {outcome}) = {pid}  balance={bal}")

    print(f"\nStored token_id (DB): {stored_token_id}")

    # 5. CTF resolution state
    print("\n-- CTF resolution state for CLOB conditionId --")
    denom = ctf.functions.payoutDenominator(cond_bytes).call()
    slots = ctf.functions.getOutcomeSlotCount(cond_bytes).call()
    print(f"  payoutDenominator = {denom}")
    print(f"  outcomeSlotCount  = {slots}")
    for i in range(max(slots, 2)):
        try:
            n = ctf.functions.payoutNumerators(cond_bytes, i).call()
            print(f"  payoutNumerator[{i}] = {n}")
        except Exception as e:
            print(f"  payoutNumerator[{i}] ERR {e}")


if __name__ == "__main__":
    main()
