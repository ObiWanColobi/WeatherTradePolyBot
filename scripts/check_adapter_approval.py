"""Read-only: check whether proxy has granted CTF.setApprovalForAll to NegRiskAdapter.

The NegRiskAdapter pulls CTF tokens from the proxy via safeBatchTransferFrom when
redeeming. Without this approval, redeemPositions reverts. This script just reads
CTF.isApprovedForAll(proxy, adapter) and prints the result.
"""
from web3 import Web3

from config import WEATHER, WALLET_FUNDER_ADDRESS


_CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
_NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

_ABI = [{
    "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "operator", "type": "address"},
    ],
    "name": "isApprovedForAll",
    "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "view",
    "type": "function",
}]


def main():
    rpc = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        raise SystemExit(f"Cannot reach Polygon RPC at {rpc}")

    proxy = Web3.to_checksum_address(WALLET_FUNDER_ADDRESS)
    print(f"RPC:     {rpc}")
    print(f"Proxy:   {proxy}")
    print(f"CTF:     {_CTF}")
    print(f"Adapter: {_NEG_RISK_ADAPTER}")

    ctf = w3.eth.contract(address=_CTF, abi=_ABI)
    approved = ctf.functions.isApprovedForAll(proxy, _NEG_RISK_ADAPTER).call()
    print(f"\nisApprovedForAll(proxy, NegRiskAdapter) = {approved}")
    if approved:
        print("Skip Task 2.1 (grant_adapter_approval) — approval already granted.")
    else:
        print("Run Task 2.1 (scripts.grant_adapter_approval) before Phase 2.2.")


if __name__ == "__main__":
    main()
