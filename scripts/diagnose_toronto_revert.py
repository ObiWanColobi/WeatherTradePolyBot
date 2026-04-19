"""Diagnose the SafeMath underflow on Toronto redemption.

Tries several hypotheses:
  1. amounts order reversed ([balance, 0] instead of [0, balance])
  2. Payout is 0 (NO was the losing side — nothing to redeem)
  3. amounts array length must be >2 for neg-risk markets
  4. questionId has wrong byte padding

Non-destructive — all eth_call, no real txs.
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

_CTF_BAL_ABI = [{"inputs":[{"name":"a","type":"address"},{"name":"id","type":"uint256"}],
                 "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
                 "stateMutability":"view","type":"function"}]
_ADAPTER_ABI = [
    {"inputs":[{"name":"_conditionId","type":"bytes32"},{"name":"amounts","type":"uint256[]"}],
     "name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"_conditionId","type":"bytes32"}],
     "name":"getDetermined","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"_conditionId","type":"bytes32"}],
     "name":"getPayout","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"_conditionId","type":"bytes32"}],
     "name":"getMetadata","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"_conditionId","type":"bytes32"},{"name":"_outcome","type":"bool"}],
     "name":"getPositionId","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]
_FACTORY_ABI = [{"inputs":[{"components":[
    {"name":"typeCode","type":"uint8"},{"name":"to","type":"address"},
    {"name":"value","type":"uint256"},{"name":"data","type":"bytes"}],
    "name":"calls","type":"tuple[]"}],"name":"proxy",
    "outputs":[{"name":"","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]


def try_call(w3, eoa, outer_hex, label):
    try:
        w3.eth.call({"from": eoa, "to": _FACTORY, "data": outer_hex})
        print(f"  [{label}] OK — no revert")
        return True
    except Exception as e:
        msg = str(e)
        if "SafeMath" in msg or "subtraction overflow" in msg:
            short = "SafeMath underflow"
        elif "execution reverted" in msg:
            # Try to pull out custom revert reason if any
            short = msg.split("execution reverted")[-1][:120]
        else:
            short = msg[:120]
        print(f"  [{label}] REVERT: {short}")
        return False


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    eoa = w3.eth.account.from_key(WALLET_PRIVATE_KEY).address
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    trade = db.get_trade_by_id(23)
    cond = trade["market_id"]
    token_id = int(trade["token_id"])
    direction = (trade.get("direction") or "NO").upper()

    kwargs = {"host": POLYMARKET_CLOB_API, "key": WALLET_PRIVATE_KEY,
              "chain_id": 137, "signature_type": WALLET_SIGNATURE_TYPE}
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    mkt = ClobClient(**kwargs).get_market(cond)
    qid = mkt["question_id"]
    qid_bytes = bytes.fromhex(qid.replace("0x", ""))

    adapter = w3.eth.contract(address=_ADAPTER, abi=_ADAPTER_ABI)
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)
    ctf = w3.eth.contract(address=_CTF, abi=_CTF_BAL_ABI)

    bal = ctf.functions.balanceOf(proxy, token_id).call()
    determined = adapter.functions.getDetermined(qid_bytes).call()
    try:
        payout = adapter.functions.getPayout(qid_bytes).call()
    except Exception as e:
        payout = f"<error: {e}>"
    try:
        metadata = adapter.functions.getMetadata(qid_bytes).call()
    except Exception as e:
        metadata = f"<error: {e}>"

    yes_pos = adapter.functions.getPositionId(qid_bytes, True).call()
    no_pos = adapter.functions.getPositionId(qid_bytes, False).call()
    yes_bal = ctf.functions.balanceOf(proxy, yes_pos).call()
    no_bal = ctf.functions.balanceOf(proxy, no_pos).call()

    print("== facts ==")
    print(f"conditionId: {cond}")
    print(f"questionId:  {qid}")
    print(f"direction:   {direction}")
    print(f"determined:  {determined}")
    print(f"payout:      {payout}")
    print(f"metadata:    {metadata}")
    print(f"stored token_id: {token_id}")
    print(f"getPositionId(q, YES): {yes_pos}  balance={yes_bal}  ({yes_bal/1e6:.4f})")
    print(f"getPositionId(q, NO):  {no_pos}  balance={no_bal}  ({no_bal/1e6:.4f})")
    print(f"balance at stored id:  {bal}  ({bal/1e6:.4f})")
    print()

    def build(amounts):
        inner_hex = adapter.functions.redeemPositions(qid_bytes, amounts)._encode_transaction_data()
        inner = bytes.fromhex(inner_hex[2:])
        call = (1, _ADAPTER, 0, inner)
        return factory.functions.proxy([call])._encode_transaction_data()

    print("== eth_call attempts ==")
    try_call(w3, eoa, build([0, bal]),        "A: [0, bal]        (plan: NO -> slot1)")
    try_call(w3, eoa, build([bal, 0]),        "B: [bal, 0]        (reversed: NO -> slot0)")
    try_call(w3, eoa, build([0, 1]),          "C: [0, 1]          (tiny NO)")
    try_call(w3, eoa, build([1, 0]),          "D: [1, 0]          (tiny YES)")
    try_call(w3, eoa, build([yes_bal, no_bal]),"E: [yes_bal, no_bal] (both)")
    try_call(w3, eoa, build([no_bal, yes_bal]),"F: [no_bal, yes_bal] (both reversed)")


if __name__ == "__main__":
    main()
