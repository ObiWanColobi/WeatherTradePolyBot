"""
On-chain claim/redeem for Polymarket resolved markets.

Polymarket uses POLY_PROXY (SignatureType=1) wallet architecture: the user's
EOA is NOT the owner of the winning conditional tokens. A per-user ProxyWallet
(deployed via CREATE2 by ProxyWalletFactory) holds the tokens, and only the
Factory (as `msg.sender == owner`) is authorized to invoke `ProxyWallet.proxy()`.

To redeem, we call:
    Factory.proxy([ProxyCall(CallType.CALL, CTF, 0, redeemPositions_calldata)])

The Factory derives the caller's proxy address from `_msgSender()` via CREATE2
and forwards the calls. The EOA signs the outer tx.
"""
import json
import os

from web3 import Web3

# Polygon contract addresses
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_PROXY_FACTORY_ADDRESS = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
_CHAIN_ID = 137

# CallType enum from ProxyWalletLib: INVALID=0, CALL=1, DELEGATECALL=2
_CALL_TYPE_CALL = 1

# Minimal ABI for ProxyWalletFactory.proxy((uint8,address,uint256,bytes)[])
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

# Load CTF ABI from adjacent file
_ABI_PATH = os.path.join(os.path.dirname(__file__), "abi", "conditional_tokens.json")


class Claimer:
    """Handles on-chain claiming of winning conditional tokens via Polymarket proxy."""

    def __init__(self, rpc_url: str, private_key: str):
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self._w3.is_connected():
            raise ConnectionError(f"Cannot connect to Polygon RPC at {rpc_url}")

        self._account = self._w3.eth.account.from_key(private_key)
        self._address = self._account.address

        with open(_ABI_PATH) as f:
            ctf_abi = json.load(f)
        self._ctf = self._w3.eth.contract(
            address=self._w3.to_checksum_address(_CTF_ADDRESS),
            abi=ctf_abi,
        )
        self._factory = self._w3.eth.contract(
            address=self._w3.to_checksum_address(_PROXY_FACTORY_ADDRESS),
            abi=_FACTORY_ABI,
        )

    def get_matic_balance(self) -> float:
        """Return MATIC balance in human-readable units."""
        wei = self._w3.eth.get_balance(self._address)
        return wei / 1e18

    def is_condition_resolved(self, condition_id: str) -> bool | None:
        """
        Return True if the UMA oracle has reported this condition on-chain.

        Uses CTF.payoutDenominator(conditionId): non-zero once the condition
        has been resolved via reportPayouts(). Zero means the oracle hasn't
        posted yet and redeemPositions() will revert with "result for
        condition not received yet".

        Returns:
            True  — oracle has reported, safe to call claim_winnings()
            False — oracle has NOT reported, claim would revert
            None  — RPC call failed, caller should treat as transient error
        """
        try:
            cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            denom = self._ctf.functions.payoutDenominator(cond_bytes).call()
            return denom > 0
        except Exception as e:
            print(f"[claimer] is_condition_resolved RPC error: {e}")
            return None

    def claim_winnings(self, condition_id: str, index_sets: list[int]) -> str | None:
        """
        Redeem winning CTF shares held by the user's Polymarket proxy wallet.

        Encodes `CTF.redeemPositions(USDC, 0, conditionId, indexSets)` as
        calldata and submits it via `ProxyWalletFactory.proxy([ProxyCall(...)])`.
        The Factory resolves the caller's proxy wallet (CREATE2 derived from
        msg.sender) and forwards the call.

        Args:
            condition_id: Market condition ID (hex bytes32).
            index_sets: [1] for YES tokens, [2] for NO tokens.

        Returns:
            Transaction hash hex string, or None on failure.
        """
        try:
            cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))

            # 1. Encode the inner CTF.redeemPositions(...) calldata.
            # Use _encode_transaction_data() which is stable across web3.py v6/v7.
            inner_fn = self._ctf.functions.redeemPositions(
                self._w3.to_checksum_address(_USDC_ADDRESS),
                b"\x00" * 32,   # parentCollectionId = bytes32(0)
                cond_bytes,
                index_sets,
            )
            inner_hex = inner_fn._encode_transaction_data()
            inner_bytes = bytes.fromhex(inner_hex[2:] if inner_hex.startswith("0x") else inner_hex)

            # 2. Wrap as a ProxyCall directed at the CTF contract
            proxy_call = (
                _CALL_TYPE_CALL,
                self._w3.to_checksum_address(_CTF_ADDRESS),
                0,
                inner_bytes,
            )

            # 3. Build the outer Factory.proxy([call]) transaction
            nonce = self._w3.eth.get_transaction_count(self._address)
            tx = self._factory.functions.proxy([proxy_call]).build_transaction({
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
