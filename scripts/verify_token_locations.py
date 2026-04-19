"""
Find where the tokens actually live.

Prior diagnostic showed neither computed positionId matches the stored
token_id. This script queries balanceOf directly at the stored token_id
across multiple candidate holders to locate where the winning tokens
actually are:
  - EOA
  - Funder / proxy wallet
  - The market's operator / CLOB contract (if tokens are held in escrow)

Also parses token_id and verifies its format.
"""
import sqlite3
import sys
from web3 import Web3

from config import WEATHER, WALLET_FUNDER_ADDRESS, WALLET_PRIVATE_KEY

_CTF  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

_CTF_ABI = [
    {"inputs": [{"type": "address"}, {"type": "uint256"}],
     "name": "balanceOf", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# Polymarket contracts (might hold escrowed positions)
_POLY_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # CTF Exchange
_POLY_NEG_RISK = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # Neg-risk adapter


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"cannot connect to {rpc}")
        sys.exit(1)

    ctf = w3.eth.contract(address=_CTF, abi=_CTF_ABI)

    acct = w3.eth.account.from_key(WALLET_PRIVATE_KEY)
    eoa = acct.address
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)

    print(f"EOA:      {eoa}")
    print(f"Proxy:    {proxy}")
    print(f"Exchange: {_POLY_EXCHANGE}")
    print(f"NegRisk:  {_POLY_NEG_RISK}")
    print()

    conn = sqlite3.connect("weather_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT id, market_name, direction, token_id "
                "FROM trades WHERE token_id IS NOT NULL AND status != 'closed'")
    rows = cur.fetchall()
    conn.close()

    holders = [
        ("EOA",      eoa),
        ("Proxy",    proxy),
        ("Exchange", Web3.to_checksum_address(_POLY_EXCHANGE)),
        ("NegRisk",  Web3.to_checksum_address(_POLY_NEG_RISK)),
    ]

    for tid, name, direction, token_id_str in rows:
        try:
            token_id = int(token_id_str)
        except Exception as e:
            print(f"#{tid} bad token_id: {token_id_str!r} ({e})")
            continue

        # Token ID format check
        hex_rep = hex(token_id)
        bits = token_id.bit_length()

        print(f"#{tid} {direction:3s} {name[:50]}")
        print(f"  token_id = {token_id_str}")
        print(f"  hex      = {hex_rep}  ({bits} bits)")
        for label, addr in holders:
            try:
                bal = ctf.functions.balanceOf(addr, token_id).call()
                marker = "  <-- HERE" if bal > 0 else ""
                print(f"  {label:9s} balance: {bal/1e6:>10.4f}{marker}")
            except Exception as e:
                print(f"  {label:9s} ERROR: {e}")
        print()


if __name__ == "__main__":
    main()
