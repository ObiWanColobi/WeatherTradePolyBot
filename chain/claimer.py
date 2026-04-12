"""
On-chain claim/redeem for Polymarket resolved markets.

Uses web3.py to call redeemPositions() on the Conditional Tokens Framework
(CTF) contract on Polygon. Stateless — receives parameters, returns results.
"""
import json
import os

from web3 import Web3

# Polygon contract addresses
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_CHAIN_ID = 137

# Load ABI from adjacent file
_ABI_PATH = os.path.join(os.path.dirname(__file__), "abi", "conditional_tokens.json")


class Claimer:
    """Handles on-chain claiming of winning conditional tokens."""

    def __init__(self, rpc_url: str, private_key: str):
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self._w3.is_connected():
            raise ConnectionError(f"Cannot connect to Polygon RPC at {rpc_url}")

        self._account = self._w3.eth.account.from_key(private_key)
        self._address = self._account.address

        with open(_ABI_PATH) as f:
            abi = json.load(f)
        self._ctf = self._w3.eth.contract(
            address=self._w3.to_checksum_address(_CTF_ADDRESS),
            abi=abi,
        )

    def get_matic_balance(self) -> float:
        """Return MATIC balance in human-readable units."""
        wei = self._w3.eth.get_balance(self._address)
        return wei / 1e18

    def claim_winnings(self, condition_id: str, index_sets: list[int]) -> str | None:
        """
        Call CTF redeemPositions() to claim winning shares.

        Args:
            condition_id: Market condition ID (hex bytes32).
            index_sets: [1] for YES tokens, [2] for NO tokens.

        Returns:
            Transaction hash hex string, or None on failure.
        """
        try:
            nonce = self._w3.eth.get_transaction_count(self._address)

            # Convert condition_id string to bytes32
            cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))

            tx = self._ctf.functions.redeemPositions(
                self._w3.to_checksum_address(_USDC_ADDRESS),
                b"\x00" * 32,   # parentCollectionId = bytes32(0)
                cond_bytes,
                index_sets,
            ).build_transaction({
                "chainId": _CHAIN_ID,
                "from": self._address,
                "nonce": nonce,
                "gasPrice": self._w3.eth.gas_price,
            })

            signed = self._w3.eth.account.sign_transaction(tx, private_key=self._account.key)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)

        except Exception as e:
            print(f"[claimer] claim_winnings failed: {e}")
            return None

    def check_tx_status(self, tx_hash: str) -> str:
        """
        Check transaction receipt status.

        Returns:
            "confirmed" — tx mined and succeeded (status=1)
            "failed"    — tx mined but reverted (status=0)
            "pending"   — no receipt yet
        """
        try:
            receipt = self._w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                return "pending"
            return "confirmed" if receipt["status"] == 1 else "failed"
        except Exception:
            return "pending"
