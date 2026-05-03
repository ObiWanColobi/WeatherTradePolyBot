"""
Wrap any USDC.e at the Polymarket proxy into pUSD via the Collateral Onramp.

After CLOB V2 (cutover 2026-04-28) Polymarket's settlement collateral is pUSD.
The on-chain claim path (chain/claimer.py) still unwraps via the legacy
WrappedCollateral contract which produces USDC.e — so post-claim proceeds
land at the proxy as USDC.e and must be wrapped to pUSD before the bot can
trade with them. The polymarket.com UI auto-wraps; API-only users must call
Onramp.wrap() themselves.

This script:
  1. Reads the proxy's USDC.e balance.
  2. Ensures the proxy has approved the Onramp to spend USDC.e (one-time).
  3. Calls Onramp.wrap(balance) from the proxy via Factory.proxy(...) so the
     wrapped pUSD lands at the proxy address.

All transactions are sent by the EOA but their effects target the proxy
(that's what ProxyWalletFactory.proxy is for). Costs MATIC for one or two
transactions.

Usage:
    python scripts/wrap_usdce_to_pusd.py                # wrap full balance
    python scripts/wrap_usdce_to_pusd.py <amount_usdc>  # wrap exact amount
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3

from config import (
    WALLET_PRIVATE_KEY, WALLET_FUNDER_ADDRESS, WEATHER,
    POLY_PUSD_ADDRESS, POLY_USDC_E_ADDRESS, POLY_COLLATERAL_ONRAMP,
)


_PROXY_FACTORY_ADDRESS = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
_CALL_TYPE_CALL = 1
_CHAIN_ID = 137

_FACTORY_ABI = [{
    "inputs": [{
        "components": [
            {"internalType": "enum ProxyWalletLib.CallType", "name": "typeCode", "type": "uint8"},
            {"internalType": "address payable", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
        ],
        "internalType": "struct ProxyWalletLib.ProxyCall[]",
        "name": "calls",
        "type": "tuple[]",
    }],
    "name": "proxy",
    "outputs": [{"internalType": "bytes[]", "name": "returnValues", "type": "bytes[]"}],
    "stateMutability": "payable",
    "type": "function",
}]

_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

_ONRAMP_ABI = [{
    "name": "wrap", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "amount", "type": "uint256"}], "outputs": [],
}]

_MAX_UINT = 2**256 - 1


def _build_proxy_call(to: str, calldata: bytes) -> tuple:
    return (_CALL_TYPE_CALL, Web3.to_checksum_address(to), 0, calldata)


def main():
    if not WALLET_PRIVATE_KEY:
        print("ERROR: WALLET_PRIVATE_KEY not set"); sys.exit(1)
    if not WALLET_FUNDER_ADDRESS:
        print("ERROR: WALLET_FUNDER_ADDRESS (proxy) not set"); sys.exit(1)

    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"ERROR: cannot reach Polygon RPC at {rpc}"); sys.exit(1)

    acct = w3.eth.account.from_key(WALLET_PRIVATE_KEY)
    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)
    onramp_addr = Web3.to_checksum_address(POLY_COLLATERAL_ONRAMP)
    usdce_addr = Web3.to_checksum_address(POLY_USDC_E_ADDRESS)
    pusd_addr = Web3.to_checksum_address(POLY_PUSD_ADDRESS)

    usdce = w3.eth.contract(address=usdce_addr, abi=_ERC20_ABI)
    pusd = w3.eth.contract(address=pusd_addr, abi=_ERC20_ABI)
    onramp = w3.eth.contract(address=onramp_addr, abi=_ONRAMP_ABI)
    factory = w3.eth.contract(
        address=Web3.to_checksum_address(_PROXY_FACTORY_ADDRESS),
        abi=_FACTORY_ABI,
    )

    bal_usdce = usdce.functions.balanceOf(proxy).call()
    bal_pusd = pusd.functions.balanceOf(proxy).call()
    print(f"Proxy:        {proxy}")
    print(f"USDC.e:       {bal_usdce / 1e6:.6f}")
    print(f"pUSD:         {bal_pusd / 1e6:.6f}")

    if bal_usdce == 0:
        print("\nNothing to wrap."); return

    if len(sys.argv) > 1:
        amount_raw = int(float(sys.argv[1]) * 1_000_000)
        if amount_raw > bal_usdce:
            print(f"ERROR: requested {amount_raw / 1e6} > balance {bal_usdce / 1e6}"); sys.exit(1)
    else:
        amount_raw = bal_usdce

    print(f"\nWrapping {amount_raw / 1e6:.6f} USDC.e -> pUSD via Onramp...")

    allow = usdce.functions.allowance(proxy, onramp_addr).call()
    print(f"Current proxy.allowance(Onramp) = {allow / 1e6:.6f}")

    calls = []
    if allow < amount_raw:
        approve_data = usdce.functions.approve(onramp_addr, _MAX_UINT)._encode_transaction_data()
        approve_inner = bytes.fromhex(approve_data[2:] if approve_data.startswith("0x") else approve_data)
        calls.append(_build_proxy_call(usdce_addr, approve_inner))
        print("  + approve(Onramp, MAX) bundled")

    wrap_data = onramp.functions.wrap(amount_raw)._encode_transaction_data()
    wrap_inner = bytes.fromhex(wrap_data[2:] if wrap_data.startswith("0x") else wrap_data)
    calls.append(_build_proxy_call(onramp_addr, wrap_inner))
    print("  + wrap(amount) bundled")

    confirm = input("\nType YES to send: ").strip()
    if confirm != "YES":
        print("Aborted."); return

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    tx = factory.functions.proxy(calls).build_transaction({
        "chainId": _CHAIN_ID,
        "from": acct.address,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw).hex()
    print(f"\nTx submitted: 0x{txh.lstrip('0x')}")
    print("Polygonscan: https://polygonscan.com/tx/" + (txh if txh.startswith("0x") else "0x" + txh))

    print("Waiting for receipt...")
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=300)
    print(f"Status: {'SUCCESS' if receipt.status == 1 else 'FAILED'}  gasUsed={receipt.gasUsed}")

    if receipt.status == 1:
        new_usdce = usdce.functions.balanceOf(proxy).call()
        new_pusd = pusd.functions.balanceOf(proxy).call()
        print(f"\nUSDC.e: {bal_usdce / 1e6:.6f} -> {new_usdce / 1e6:.6f}")
        print(f"pUSD:   {bal_pusd / 1e6:.6f} -> {new_pusd / 1e6:.6f}")


if __name__ == "__main__":
    main()
