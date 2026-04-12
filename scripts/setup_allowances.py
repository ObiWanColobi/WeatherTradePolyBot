"""
One-time USDC + CTF allowance setup for Polymarket CLOB trading.

This script approves the Polymarket exchange contracts to spend your USDC
and conditional tokens on Polygon mainnet. Required before live trading.

Usage:
    python scripts/setup_allowances.py

Requires:
    - WALLET_PRIVATE_KEY set in .env
    - MATIC in your wallet for gas fees (~0.01 MATIC per approval)
    - web3 package installed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from web3.constants import MAX_INT

PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
if not PRIVATE_KEY:
    print("ERROR: WALLET_PRIVATE_KEY not set in .env")
    sys.exit(1)

RPC_URL = "https://polygon-rpc.com"
CHAIN_ID = 137

# Polygon contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Polymarket exchange contracts that need approval
EXCHANGE_CONTRACTS = [
    ("CTF Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]

ERC1155_APPROVAL_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    }
]


def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        sys.exit(1)

    account = w3.eth.account.from_key(PRIVATE_KEY)
    pub_key = account.address
    print(f"Wallet: {pub_key}")

    matic_balance = w3.eth.get_balance(pub_key) / 1e18
    print(f"MATIC balance: {matic_balance:.4f}")
    if matic_balance < 0.01:
        print("WARNING: Low MATIC balance — you need gas for approval transactions")

    usdc = w3.eth.contract(
        address=w3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI
    )
    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI
    )

    nonce = w3.eth.get_transaction_count(pub_key)

    print(f"\nApproving {len(EXCHANGE_CONTRACTS)} contracts (2 txns each)...\n")

    for name, contract_addr in EXCHANGE_CONTRACTS:
        target = w3.to_checksum_address(contract_addr)

        # USDC approval
        print(f"  [{name}] Approving USDC spend...")
        tx = usdc.functions.approve(target, int(MAX_INT, 0)).build_transaction({
            "chainId": CHAIN_ID,
            "from": pub_key,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        status = "OK" if receipt["status"] == 1 else "FAILED"
        print(f"  [{name}] USDC: {status}  tx={receipt['transactionHash'].hex()[:16]}...")
        nonce += 1

        # CTF approval
        print(f"  [{name}] Approving CTF tokens...")
        tx = ctf.functions.setApprovalForAll(target, True).build_transaction({
            "chainId": CHAIN_ID,
            "from": pub_key,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        status = "OK" if receipt["status"] == 1 else "FAILED"
        print(f"  [{name}] CTF:  {status}  tx={receipt['transactionHash'].hex()[:16]}...")
        nonce += 1

    print("\nAll approvals complete. You can now run the bot in live mode.")
    print("  Set TRADING_MODE=live in your .env file to enable.")


if __name__ == "__main__":
    main()
