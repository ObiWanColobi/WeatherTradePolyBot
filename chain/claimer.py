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

from config import WALLET_FUNDER_ADDRESS as _PROXY_ADDRESS

# Polygon contract addresses
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Polymarket migrated active settlement to pUSD ~2026-04-30 (linked to auto-redeem).
# Same proxy address; balances may live in either token. Sum both for the
# chain-direct balance fallback so the bot doesn't see $0 just because the
# user's funds were swept to pUSD. Full migration scope is pinned — this is the
# minimal change to keep the on-chain fallback honest.
_PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_WCOL_ADDRESS = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"
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
_WCOL_ABI_PATH = os.path.join(os.path.dirname(__file__), "abi", "wrapped_collateral.json")


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
        with open(_WCOL_ABI_PATH) as f:
            wcol_abi = json.load(f)
        self._wcol = self._w3.eth.contract(
            address=self._w3.to_checksum_address(_WCOL_ADDRESS),
            abi=wcol_abi,
        )
        self._factory = self._w3.eth.contract(
            address=self._w3.to_checksum_address(_PROXY_FACTORY_ADDRESS),
            abi=_FACTORY_ABI,
        )

    def get_matic_balance(self) -> float:
        """Return MATIC balance in human-readable units."""
        wei = self._w3.eth.get_balance(self._address)
        return wei / 1e18

    def get_usdc_balance(self, address: str) -> float | None:
        """Read on-chain spendable balance of `address`: USDC.e + pUSD.

        Both are 6-decimal stablecoins pegged 1:1; Polymarket settlement
        currency moved from USDC.e to pUSD ~2026-04-30. Sum so the fallback
        keeps working through (and after) the migration. Returns None only
        when the RPC call itself fails — partial reads return what we got.
        """
        abi = [{
            "constant": True,
            "inputs":   [{"name": "owner", "type": "address"}],
            "name":     "balanceOf",
            "outputs":  [{"name": "", "type": "uint256"}],
            "type":     "function",
        }]
        addr = self._w3.to_checksum_address(address)
        total_raw = 0
        any_ok = False
        for token_addr in (_USDC_ADDRESS, _PUSD_ADDRESS):
            try:
                token = self._w3.eth.contract(
                    address=self._w3.to_checksum_address(token_addr), abi=abi,
                )
                total_raw += token.functions.balanceOf(addr).call()
                any_ok = True
            except Exception as e:
                print(f"[claimer] get_usdc_balance error on {token_addr[:10]}…: {e}")
        return (total_raw / 1e6) if any_ok else None

    def get_token_balance(self, proxy_address: str, token_id: int) -> int:
        """Raw uint256 CTF ERC-1155 balance of proxy at token_id. 0 on RPC error."""
        try:
            return self._ctf.functions.balanceOf(
                self._w3.to_checksum_address(proxy_address), int(token_id),
            ).call()
        except Exception as e:
            print(f"[claimer] get_token_balance error: {e}")
            return 0

    def is_condition_redeemable(self, condition_id: str) -> bool | None:
        """Return True if CTF has resolved this condition (payoutDenominator > 0).

        For neg-risk weather markets this is sufficient — we're redeeming against
        the CTF directly with wcol as collateral, bypassing the adapter.
        """
        try:
            cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            denom = self._ctf.functions.payoutDenominator(cond_bytes).call()
            return denom > 0
        except Exception as e:
            print(f"[claimer] is_condition_redeemable RPC error: {e}")
            return None

    def claim_winnings(
        self,
        condition_id: str,
        expected_wcol: int,
    ) -> str | None:
        """Redeem winning positions via bundled CTF+wcol tx (neg-risk bypass path).

        For neg-risk markets, the NegRiskAdapter is often in a half-resolved state
        that makes its redeemPositions revert. We bypass it by calling the CTF
        directly with wcol as collateral (which is what the adapter does
        internally), then unwrapping wcol to USDC. Both inner calls are bundled
        into a single Factory.proxy tx.

        Args:
            condition_id: CLOB conditionId (bytes32 hex).
            expected_wcol: Raw uint256 wcol amount to unwrap. Equal to the proxy's
                balance at the winning position id (for binary markets with
                payoutDenominator=1).

        Returns:
            Transaction hash hex string, or None on failure.
        """
        try:
            cond_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            proxy_addr = self._w3.to_checksum_address(_PROXY_ADDRESS)

            redeem_hex = self._ctf.functions.redeemPositions(
                self._w3.to_checksum_address(_WCOL_ADDRESS),
                b"\x00" * 32,
                cond_bytes,
                [1, 2],
            )._encode_transaction_data()
            redeem_inner = bytes.fromhex(redeem_hex[2:] if redeem_hex.startswith("0x") else redeem_hex)

            unwrap_hex = self._wcol.functions.unwrap(
                proxy_addr, int(expected_wcol),
            )._encode_transaction_data()
            unwrap_inner = bytes.fromhex(unwrap_hex[2:] if unwrap_hex.startswith("0x") else unwrap_hex)

            calls = [
                (_CALL_TYPE_CALL, self._w3.to_checksum_address(_CTF_ADDRESS),  0, redeem_inner),
                (_CALL_TYPE_CALL, self._w3.to_checksum_address(_WCOL_ADDRESS), 0, unwrap_inner),
            ]

            nonce = self._w3.eth.get_transaction_count(self._address, "pending")
            tx = self._factory.functions.proxy(calls).build_transaction({
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
        """Check receipt AND require a CTF.PayoutRedemption event with payout>0.

        Rationale: the old version returned 'confirmed' on any status=1 receipt.
        That let phantom redemptions (status=1, payout=0, no USDC movement) get
        marked claim_confirmed in the DB. This version parses logs — if no
        PayoutRedemption event from CTF with payout>0 is present, the tx is
        treated as failed so the retry path kicks in.
        """
        try:
            receipt = self._w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                return "pending"
            if receipt["status"] != 1:
                return "failed"

            ctf_addr = self._w3.to_checksum_address(_CTF_ADDRESS).lower()
            event = self._ctf.events.PayoutRedemption()
            for log in receipt.get("logs", []):
                if log["address"].lower() != ctf_addr:
                    continue
                try:
                    parsed = event.process_log(log)
                except Exception:
                    continue
                if parsed["args"]["payout"] > 0:
                    return "confirmed"

            print(f"[claimer] tx {tx_hash[:16]}... status=1 but no CTF.PayoutRedemption "
                  f"payout>0 — treating as failed (phantom)")
            return "failed"
        except Exception:
            return "pending"
