"""Read-only: simulate the full bundled claim (CTF.redeemPositions + wcol.unwrap)
for Toronto #23 as one Factory.proxy tx. No signing, no broadcast.
"""
from web3 import Web3

from config import (
    WEATHER, WALLET_PRIVATE_KEY, WALLET_FUNDER_ADDRESS,
)
import db


_FACTORY = Web3.to_checksum_address("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052")
_CTF     = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_WCOL    = Web3.to_checksum_address("0x3A3BD7bb9528E159577F7C2e685CC81A765002E2")

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
_FACTORY_ABI = [{"inputs":[{"components":[
    {"name":"typeCode","type":"uint8"},{"name":"to","type":"address"},
    {"name":"value","type":"uint256"},{"name":"data","type":"bytes"}],
    "name":"calls","type":"tuple[]"}],"name":"proxy",
    "outputs":[{"name":"","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    eoa = w3.eth.account.from_key(WALLET_PRIVATE_KEY).address
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    trade = db.get_trade_by_id(23)
    cond = trade["market_id"]
    token_id = int(trade["token_id"])
    cond_bytes = bytes.fromhex(cond.replace("0x", ""))

    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)
    wcol = w3.eth.contract(address=_WCOL, abi=_WCOL_ABI)
    factory = w3.eth.contract(address=_FACTORY, abi=_FACTORY_ABI)

    expected = ctf.functions.balanceOf(proxy, token_id).call()
    assert expected > 0, "no balance at winning token_id"
    print(f"conditionId:   {cond}")
    print(f"token_id:      {token_id}")
    print(f"expected wcol: {expected} ({expected/1e6:.4f} USDC)")

    redeem_hex = ctf.functions.redeemPositions(
        _WCOL, b"\x00"*32, cond_bytes, [1, 2]
    )._encode_transaction_data()
    redeem_inner = bytes.fromhex(redeem_hex[2:])

    unwrap_hex = wcol.functions.unwrap(proxy, expected)._encode_transaction_data()
    unwrap_inner = bytes.fromhex(unwrap_hex[2:])

    calls = [
        (1, _CTF,  0, redeem_inner),
        (1, _WCOL, 0, unwrap_inner),
    ]
    outer = factory.functions.proxy(calls)._encode_transaction_data()

    print(f"from:          {eoa}")
    print(f"to:            {_FACTORY}")
    print(f"calldata size: {len(outer)//2 - 1} bytes")
    print()

    result = w3.eth.call({"from": eoa, "to": _FACTORY, "data": outer})
    print(f"eth_call OK — returned {len(result)} bytes")


if __name__ == "__main__":
    main()
