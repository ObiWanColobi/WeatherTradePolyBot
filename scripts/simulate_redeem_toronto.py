"""Read-only: eth_call simulation of the correct NegRiskAdapter redemption for Toronto #23.

Proves the call path does not revert. This does NOT prove USDC will move — only
Phase 2 (a real live tx) can prove that. Phase 1 gatekeeper only.
"""
from web3 import Web3

from config import (
    WEATHER, POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS,
)
from py_clob_client.client import ClobClient
import db


_EOA = None  # derived from private key below
_FACTORY = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

_CTF_BAL_ABI = [{
    "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]
_ADAPTER_ABI = [
    {"inputs": [{"name": "_conditionId", "type": "bytes32"},
                {"name": "amounts", "type": "uint256[]"}],
     "name": "redeemPositions", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_conditionId", "type": "bytes32"}],
     "name": "getDetermined", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
]
_FACTORY_ABI = [{
    "inputs": [{
        "components": [
            {"name": "typeCode", "type": "uint8"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "name": "calls", "type": "tuple[]",
    }],
    "name": "proxy",
    "outputs": [{"name": "", "type": "bytes[]"}],
    "stateMutability": "payable", "type": "function",
}]

TORONTO_TRADE_ID = 23


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        raise SystemExit(f"Cannot reach Polygon RPC at {rpc}")
    eoa = w3.eth.account.from_key(WALLET_PRIVATE_KEY).address
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    trade = db.get_trade_by_id(TORONTO_TRADE_ID)
    if trade is None:
        raise SystemExit(f"Trade #{TORONTO_TRADE_ID} not found in DB")
    cond = trade["market_id"]
    token_id = int(trade["token_id"])
    direction = (trade.get("direction") or "NO").upper()

    # 1. questionId from CLOB
    kwargs = {
        "host": POLYMARKET_CLOB_API, "key": WALLET_PRIVATE_KEY,
        "chain_id": 137, "signature_type": WALLET_SIGNATURE_TYPE,
    }
    if WALLET_FUNDER_ADDRESS:
        kwargs["funder"] = WALLET_FUNDER_ADDRESS
    client = ClobClient(**kwargs)
    mkt = client.get_market(cond)
    qid = mkt["question_id"]

    print(f"Trade       #{TORONTO_TRADE_ID}  {trade['market_name'][:60]}")
    print(f"conditionId {cond}")
    print(f"questionId  {qid}")
    print(f"token_id    {token_id}")
    print(f"direction   {direction}")
    print(f"EOA         {eoa}")
    print(f"Proxy       {proxy}")

    # 2. Balance in proxy at the token_id
    ctf = w3.eth.contract(address=_CTF, abi=_CTF_BAL_ABI)
    bal = ctf.functions.balanceOf(proxy, token_id).call()
    print(f"\nproxy balanceOf(token_id) = {bal} raw ({bal/1e6:.4f} shares)")
    if bal == 0:
        print("WARNING: proxy has 0 tokens at this token_id. Either already redeemed, "
              "or token_id mismatch. Continuing simulation anyway so the call-path "
              "itself is still checked — but expect a revert.")

    # 3. getDetermined sanity check
    adapter = w3.eth.contract(address=_ADAPTER, abi=_ADAPTER_ABI)
    qid_bytes = bytes.fromhex(qid.replace("0x", ""))
    determined = adapter.functions.getDetermined(qid_bytes).call()
    print(f"NegRiskAdapter.getDetermined(qid) = {determined}")
    if not determined:
        print("WARNING: adapter says not determined — redemption will revert.")

    # 4. Build redeemPositions(questionId, amounts)
    amounts = [bal, 0] if direction == "YES" else [0, bal]
    inner_hex = adapter.functions.redeemPositions(qid_bytes, amounts)._encode_transaction_data()
    inner_bytes = bytes.fromhex(inner_hex[2:])
    print(f"\nredeemPositions calldata (hex, first 80): 0x{inner_hex[2:82]}...")
    print(f"amounts = {amounts}  (slot 0 = YES, slot 1 = NO)")

    # 5. Factory.proxy([(CALL, adapter, 0, inner)])
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)
    call = (1, _ADAPTER, 0, inner_bytes)
    outer_hex = factory.functions.proxy([call])._encode_transaction_data()

    # 6. eth_call from EOA
    print("\n-- eth_call --")
    try:
        result = w3.eth.call({
            "from": eoa,
            "to":   _FACTORY,
            "data": outer_hex,
        })
        print(f"OK — return bytes (hex, first 80): 0x{result.hex()[:80]}...")
        print("\nCall path does NOT revert. Safe to proceed to Phase 2 (live tx).")
    except Exception as e:
        print(f"REVERT: {e}")
        print("\nDo NOT proceed to Phase 2. Diagnose revert reason first.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
