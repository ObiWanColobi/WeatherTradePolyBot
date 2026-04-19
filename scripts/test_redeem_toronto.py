"""Live: redeem Toronto #23 via bundled (CTF.redeemPositions + wcol.unwrap).

One Factory.proxy tx with two inner calls:
  1. CTF.redeemPositions(wcol, 0x0, conditionId, [1, 2])  -> wcol to proxy
  2. wcol.unwrap(proxy, expected_wcol)                     -> USDC to proxy

Interactive: simulates first, pauses for ENTER, then submits. Prints balance delta.
"""
import time
from web3 import Web3

from config import (
    WEATHER, WALLET_PRIVATE_KEY, WALLET_FUNDER_ADDRESS,
)
import db


_FACTORY = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
_CTF     = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_WCOL    = Web3.to_checksum_address("0x3A3BD7bb9528E159577F7C2e685CC81A765002E2")
_USDC    = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

_CTF_ABI = [
    {"inputs":[
        {"name":"collateralToken","type":"address"},
        {"name":"parentCollectionId","type":"bytes32"},
        {"name":"conditionId","type":"bytes32"},
        {"name":"indexSets","type":"uint256[]"}],
     "name":"redeemPositions","outputs":[],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"a","type":"address"},{"name":"id","type":"uint256"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]
_WCOL_ABI = [{"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
              "name":"unwrap","outputs":[],"stateMutability":"nonpayable","type":"function"}]
_USDC_ABI = [{"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
              "outputs":[{"name":"","type":"uint256"}],
              "stateMutability":"view","type":"function"}]
_FACTORY_ABI = [{"inputs":[{"components":[
    {"name":"typeCode","type":"uint8"},{"name":"to","type":"address"},
    {"name":"value","type":"uint256"},{"name":"data","type":"bytes"}],
    "name":"calls","type":"tuple[]"}],"name":"proxy",
    "outputs":[{"name":"","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]

TRADE_ID = 23


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = w3.eth.account.from_key(WALLET_PRIVATE_KEY)
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    trade = db.get_trade_by_id(TRADE_ID)
    cond = trade["market_id"]
    token_id = int(trade["token_id"])
    cond_bytes = bytes.fromhex(cond.replace("0x", ""))

    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)
    wcol = w3.eth.contract(address=_WCOL, abi=_WCOL_ABI)
    usdc = w3.eth.contract(address=_USDC, abi=_USDC_ABI)
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)

    expected = ctf.functions.balanceOf(proxy, token_id).call()
    pre_usdc = usdc.functions.balanceOf(proxy).call()
    assert expected > 0, "no balance at winning token_id"
    print(f"Trade #{TRADE_ID} — Toronto NO")
    print(f"  conditionId:   {cond}")
    print(f"  expected wcol: {expected} ({expected/1e6:.4f})")
    print(f"  pre USDC:      {pre_usdc} ({pre_usdc/1e6:.4f})")
    print()

    redeem_inner = bytes.fromhex(ctf.functions.redeemPositions(
        _WCOL, b"\x00"*32, cond_bytes, [1, 2]
    )._encode_transaction_data()[2:])
    unwrap_inner = bytes.fromhex(wcol.functions.unwrap(
        proxy, expected
    )._encode_transaction_data()[2:])
    calls = [(1, _CTF, 0, redeem_inner), (1, _WCOL, 0, unwrap_inner)]

    outer = factory.functions.proxy(calls)._encode_transaction_data()
    w3.eth.call({"from": acct.address, "to": factory.address, "data": outer})
    print("simulation: OK (no revert)")
    print()
    print("ACTION: this will submit a real Polygon tx that moves ~$23.54 USDC")
    print("        from wrapped-CTF positions into the proxy's USDC balance.")
    print()

    input("Press ENTER to submit, Ctrl-C to abort: ")

    tx = factory.functions.proxy(calls).build_transaction({
        "chainId": 137,
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=WALLET_PRIVATE_KEY)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = "0x" + h.hex() if isinstance(h, (bytes, bytearray)) else str(h)
    print(f"tx: {tx_hex}")
    print(f"polygonscan: https://polygonscan.com/tx/{tx_hex}")

    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=240)
    print(f"status: {rcpt['status']}")
    print(f"logs:   {len(rcpt['logs'])}")
    print(f"gasUsed: {rcpt['gasUsed']}")

    time.sleep(5)
    post_usdc = usdc.functions.balanceOf(proxy).call()
    delta = post_usdc - pre_usdc
    print(f"post USDC: {post_usdc} ({post_usdc/1e6:.4f})")
    print(f"delta:     +{delta} (+${delta/1e6:.4f})")
    if delta <= 0:
        print()
        print("!! USDC did NOT increase. PHANTOM. Do NOT proceed to Phase 3.")
    else:
        print()
        print("OK — claim path verified end-to-end.")


if __name__ == "__main__":
    main()
