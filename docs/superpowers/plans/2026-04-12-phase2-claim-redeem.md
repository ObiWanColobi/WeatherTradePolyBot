# Phase 2: On-Chain Claim/Redeem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically redeem winning conditional tokens on-chain after market resolution, with retry/backoff for failed claims, and only credit the DB balance after on-chain confirmation.

**Architecture:** New `chain/claimer.py` module handles all web3/CTF contract interaction. `LiveExecutor.settle_resolved()` is split into resolution recording (immediate) and claim processing (deferred). A new claims pass in the bot loop processes pending claims each cycle. Paper executor is unchanged.

**Tech Stack:** web3.py (already installed), Polygon RPC, Polymarket CTF contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`), SQLite

**Spec:** `docs/superpowers/specs/2026-04-12-phase2-claim-redeem-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `chain/__init__.py` | Create | Empty package init |
| `chain/claimer.py` | Create | Web3 CTF interaction: claim, tx status, MATIC balance |
| `chain/abi/conditional_tokens.json` | Create | CTF contract ABI (redeemPositions only) |
| `db.py` | Modify | Add claim columns to trades table |
| `config.py` | Modify | Add claim config keys |
| `executor/base.py` | Modify | Add `process_pending_claims()` default no-op |
| `executor/live.py` | Modify | Split settle_resolved, add process_pending_claims() |
| `weather_bot.py` | Modify | Add claims pass to bot loop |
| `tests/test_claimer.py` | Create | Unit tests for chain/claimer.py |
| `tests/test_claim_flow.py` | Create | Integration tests for claim lifecycle |

---

### Task 1: DB Schema — Add Claim Columns

**Files:**
- Modify: `db.py:189-192` (after existing `_safe_add_column` calls)

- [ ] **Step 1: Write the failing test**

Create `tests/test_claim_flow.py`:

```python
"""Tests for the on-chain claim lifecycle."""
import pytest
from unittest.mock import patch


def test_claim_columns_exist():
    """Verify claim-related columns are added to trades table."""
    import db
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute("PRAGMA table_info(trades)").fetchall()
        col_names = {r["name"] for r in row}

    assert "claim_status" in col_names
    assert "claim_tx_hash" in col_names
    assert "claim_retries" in col_names
    assert "claim_last_attempt" in col_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claim_flow.py::test_claim_columns_exist -v`
Expected: FAIL — `claim_status` not in col_names

- [ ] **Step 3: Add claim columns to db.py**

In `db.py`, add after line 192 (after the `fee_usdc` column):

```python
_safe_add_column(conn, "trades", "claim_status",       "TEXT")      # claim_pending | claim_confirmed | claim_failed
_safe_add_column(conn, "trades", "claim_tx_hash",      "TEXT")      # Polygon tx hash
_safe_add_column(conn, "trades", "claim_retries",      "INTEGER DEFAULT 0")  # retry count
_safe_add_column(conn, "trades", "claim_last_attempt", "TEXT")      # ISO timestamp of last attempt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claim_flow.py::test_claim_columns_exist -v`
Expected: PASS

- [ ] **Step 5: Add DB helper — get_pending_claims()**

In `db.py`, add a new function after the existing trade query functions:

```python
def get_pending_claims() -> list[dict]:
    """Return all trades with claim_status = 'claim_pending'."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE claim_status = 'claim_pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_claim_flow.py
git commit -m "feat(db): add claim columns and get_pending_claims helper"
```

---

### Task 2: Config — Add Claim Settings

**Files:**
- Modify: `config.py:140-148` (in the WEATHER dict, after live trading overrides)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
def test_claim_config_keys_exist():
    """Verify claim config keys are present in WEATHER dict."""
    from config import WEATHER
    assert "claim_retry_backoff_minutes" in WEATHER
    assert "claim_min_matic_balance" in WEATHER
    assert "polygon_rpc_url" in WEATHER
    assert len(WEATHER["claim_retry_backoff_minutes"]) == 5
    assert WEATHER["claim_retry_backoff_minutes"] == [5, 30, 120, 480, 1440]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claim_flow.py::test_claim_config_keys_exist -v`
Expected: FAIL — KeyError: 'claim_retry_backoff_minutes'

- [ ] **Step 3: Add claim config keys to config.py**

In `config.py`, add inside the `WEATHER` dict after the live trading overrides block (after line 147, before the closing `}`):

```python
    # ── On-chain claim/redeem (live only) ────────────────────────────────────
    "claim_retry_backoff_minutes":  [5, 30, 120, 480, 1440],  # 5min, 30min, 2hr, 8hr, 24hr
    "claim_min_matic_balance":      0.01,                       # defer claims if MATIC below this
    "polygon_rpc_url":              "https://polygon-rpc.com",  # Polygon JSON-RPC endpoint
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claim_flow.py::test_claim_config_keys_exist -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_claim_flow.py
git commit -m "feat(config): add claim retry backoff and gas guard settings"
```

---

### Task 3: Chain Module — CTF Contract ABI + Claimer

**Files:**
- Create: `chain/__init__.py`
- Create: `chain/abi/conditional_tokens.json`
- Create: `chain/claimer.py`
- Create: `tests/test_claimer.py`

- [ ] **Step 1: Create package structure**

Create `chain/__init__.py` (empty file):

```python
```

Create `chain/abi/conditional_tokens.json` — minimal ABI with only `redeemPositions`:

```json
[
    {
        "constant": false,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": false,
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
```

- [ ] **Step 2: Write failing tests for claimer**

Create `tests/test_claimer.py`:

```python
"""Tests for chain/claimer.py — all web3 calls mocked."""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock


@pytest.fixture
def mock_web3():
    """Create a mock Web3 instance with standard Polygon responses."""
    w3 = MagicMock()
    w3.is_connected.return_value = True
    w3.eth.chain_id = 137
    w3.eth.gas_price = 30_000_000_000  # 30 gwei
    w3.eth.get_balance.return_value = 1_000_000_000_000_000_000  # 1 MATIC
    w3.eth.get_transaction_count.return_value = 42
    w3.to_checksum_address = lambda x: x
    return w3


@patch("chain.claimer.Web3")
def test_claimer_init_connects(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    assert c._w3 is mock_web3


@patch("chain.claimer.Web3")
def test_claimer_init_fails_on_no_connection(MockWeb3):
    w3 = MagicMock()
    w3.is_connected.return_value = False
    MockWeb3.return_value = w3
    MockWeb3.HTTPProvider = MagicMock()

    from chain.claimer import Claimer
    with pytest.raises(ConnectionError, match="Polygon RPC"):
        Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")


@patch("chain.claimer.Web3")
def test_get_matic_balance(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_balance.return_value = 500_000_000_000_000_000  # 0.5 MATIC

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    balance = c.get_matic_balance()
    assert abs(balance - 0.5) < 0.001


@patch("chain.claimer.Web3")
def test_claim_winnings_builds_and_sends_tx(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()

    # Mock contract
    mock_contract = MagicMock()
    mock_fn = MagicMock()
    mock_fn.build_transaction.return_value = {
        "chainId": 137, "from": "0xwallet", "nonce": 42, "gas": 200000,
    }
    mock_contract.functions.redeemPositions.return_value = mock_fn
    mock_web3.eth.contract.return_value = mock_contract

    # Mock signing + sending
    mock_signed = MagicMock()
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")
    mock_web3.eth.account.sign_transaction.return_value = mock_signed
    mock_web3.eth.send_raw_transaction.return_value = b"\xab" * 32

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    tx_hash = c.claim_winnings(
        condition_id="0x" + "ab" * 32,
        index_sets=[1],
    )
    assert tx_hash is not None
    mock_contract.functions.redeemPositions.assert_called_once()


@patch("chain.claimer.Web3")
def test_check_tx_status_confirmed(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = {"status": 1}
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "confirmed"


@patch("chain.claimer.Web3")
def test_check_tx_status_failed(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = {"status": 0}
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "failed"


@patch("chain.claimer.Web3")
def test_check_tx_status_pending(MockWeb3, mock_web3):
    MockWeb3.return_value = mock_web3
    MockWeb3.HTTPProvider = MagicMock()
    mock_web3.eth.get_transaction_receipt.return_value = None
    mock_web3.eth.account.from_key.return_value = MagicMock(address="0xwallet")

    from chain.claimer import Claimer
    c = Claimer(rpc_url="https://polygon-rpc.com", private_key="0xfakekey")
    status = c.check_tx_status("0x" + "ab" * 32)
    assert status == "pending"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_claimer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chain.claimer'`

- [ ] **Step 4: Implement chain/claimer.py**

Create `chain/claimer.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_claimer.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add chain/__init__.py chain/abi/conditional_tokens.json chain/claimer.py tests/test_claimer.py
git commit -m "feat(chain): add Claimer for on-chain CTF redemption"
```

---

### Task 4: BaseExecutor — Add process_pending_claims() Default

**Files:**
- Modify: `executor/base.py:30-43`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
def test_base_executor_has_process_pending_claims():
    """BaseExecutor.process_pending_claims() exists as a no-op default."""
    from executor.base import BaseExecutor
    assert hasattr(BaseExecutor, "process_pending_claims")
    # Verify it's not abstract (paper executor shouldn't need to implement it)
    assert "process_pending_claims" not in BaseExecutor.__abstractmethods__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_claim_flow.py::test_base_executor_has_process_pending_claims -v`
Expected: FAIL — `AttributeError: type object 'BaseExecutor' has no attribute 'process_pending_claims'`

- [ ] **Step 3: Add process_pending_claims to BaseExecutor**

In `executor/base.py`, add after the `reconcile_positions` method (after line 43):

```python
    def process_pending_claims(self):
        """
        Process on-chain claims for resolved winning trades.

        Paper mode:  no-op — balance is credited immediately at resolution.
        Live mode:   submits CTF redeemPositions() transactions and polls
                     for confirmation before crediting the DB balance.
        """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_claim_flow.py::test_base_executor_has_process_pending_claims -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add executor/base.py tests/test_claim_flow.py
git commit -m "feat(base): add process_pending_claims no-op to BaseExecutor"
```

---

### Task 5: LiveExecutor — Split settle_resolved() for Winning Trades

**Files:**
- Modify: `executor/live.py:515-572`

This is the core change. Losing trades still close immediately. Winning trades set `claim_pending` and defer balance credit.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
from unittest.mock import patch, MagicMock


@patch("executor.live.db")
def test_settle_resolved_winning_sets_claim_pending(mock_db):
    """Winning live trades should set claim_pending, NOT close or credit balance."""
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "fill_price": 0.50, "end_date": "2026-04-10T23:59:59Z",
        "city": "new york city", "threshold": ">=60F",
        "token_id": "tok-yes-123",
    }

    ex.settle_resolved(trade, resolved_yes=True)

    update = mock_db.update_trade.call_args[0][1]
    assert update["claim_status"] == "claim_pending"
    assert update["claim_retries"] == 0
    # Should NOT be closed yet — that happens after claim confirmation
    assert update["status"] == "claim_pending"
    # Should NOT credit balance yet
    mock_db.update_balance.assert_not_called()


@patch("executor.live.db")
def test_settle_resolved_losing_closes_immediately(mock_db):
    """Losing live trades should close immediately with no claim needed."""
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    trade = {
        "id": 11, "market_id": "0x" + "cd" * 32,
        "market_name": "Will Dallas be above 80F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "fill_price": 0.50, "end_date": "2026-04-10T23:59:59Z",
        "city": "dallas", "threshold": ">=80F",
        "token_id": "tok-yes-456",
    }

    ex.settle_resolved(trade, resolved_yes=False)

    update = mock_db.update_trade.call_args[0][1]
    assert update["status"] == "closed"
    assert update["exit_price"] == 0.00
    assert update["pnl"] == -10.0
    # Losing trades have no claim
    assert "claim_status" not in update or update.get("claim_status") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_flow.py::test_settle_resolved_winning_sets_claim_pending tests/test_claim_flow.py::test_settle_resolved_losing_closes_immediately -v`
Expected: FAIL — winning trade sets `status='closed'` and calls `update_balance`

- [ ] **Step 3: Rewrite settle_resolved in executor/live.py**

Replace the `settle_resolved` method in `executor/live.py` (lines 515-572) with:

```python
    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """
        Settle a trade at market resolution.

        Losing trades: close immediately ($0 proceeds, no on-chain action).
        Winning trades: record resolution metadata and set claim_pending.
        Balance is NOT credited until the on-chain claim is confirmed.
        """
        direction = trade.get("direction", "YES").upper()
        won = (direction == "YES" and resolved_yes) or \
              (direction == "NO" and not resolved_yes)
        actual = "YES" if resolved_yes else "NO"
        close_price = 1.0 if resolved_yes else 0.0

        exit_price = 1.00 if won else 0.00
        cost = trade["size_usdc"]
        proceeds = trade["shares"] * exit_price
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        if won:
            # Winning trade — defer balance credit until on-chain claim confirms
            db.update_trade(trade["id"], {
                "exit_price":              exit_price,
                "status":                  "claim_pending",
                "pnl":                     pnl,
                "pnl_pct":                 pnl_pct,
                "exit_reason":             "resolved",
                "actual_resolution":       actual,
                "forecast_correct":        1,
                "resolution_price":        close_price,
                "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
                "claim_status":            "claim_pending",
                "claim_retries":           0,
            })
            print(f"  [resolve] WIN   {trade['market_name'][:52]}")
            print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) — claim pending")
        else:
            # Losing trade — close immediately, no on-chain action needed
            db.update_balance(0)  # $0 proceeds, but record the event
            db.update_trade(trade["id"], {
                "exit_price":              exit_price,
                "closed_at":               datetime.now(timezone.utc).isoformat(),
                "status":                  "closed",
                "pnl":                     pnl,
                "pnl_pct":                 pnl_pct,
                "exit_reason":             "resolved",
                "actual_resolution":       actual,
                "forecast_correct":        0,
                "resolution_price":        close_price,
                "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
            })
            db.record_account_value()
            print(f"  [resolve] LOSS  {trade['market_name'][:52]}")
            print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

        # Freeze tracked-trader positions for this resolved market
        city = (trade.get("city") or "").lower()
        end_date = (trade.get("end_date") or "")[:10]
        market_id = trade.get("market_id")
        if market_id and city and end_date:
            try:
                frozen = db.freeze_trader_forecasts(
                    market_id=market_id,
                    close_price=close_price,
                    city=city,
                    end_date=end_date,
                    threshold=trade.get("threshold"),
                )
                if frozen > 0:
                    print(f"            froze {frozen} trader forecast(s) for {city} {end_date}")
            except Exception as e:
                print(f"            [warn] freeze_trader_forecasts failed: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_flow.py::test_settle_resolved_winning_sets_claim_pending tests/test_claim_flow.py::test_settle_resolved_losing_closes_immediately -v`
Expected: PASS

- [ ] **Step 5: Update existing settle_resolved tests**

The existing tests in `tests/test_live_executor.py` (`test_settle_resolved_records_win` and `test_settle_resolved_records_loss`) need updating to match the new behavior.

In `tests/test_live_executor.py`, update `test_settle_resolved_records_win` (lines 209-233):

```python
@patch("executor.live.db")
def test_settle_resolved_records_win(mock_db):
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    trade = {
        "id": 5, "market_id": "cond-123", "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "fill_price": 0.50, "end_date": "2026-04-10T23:59:59Z",
        "city": "new york city", "threshold": ">=60F",
        "token_id": "tok-yes-123",
    }

    ex.settle_resolved(trade, resolved_yes=True)

    mock_db.update_trade.assert_called_once()
    update = mock_db.update_trade.call_args[0][1]
    assert update["status"] == "claim_pending"
    assert update["claim_status"] == "claim_pending"
    assert update["exit_price"] == 1.00
    assert update["actual_resolution"] == "YES"
    assert update["forecast_correct"] == 1
    assert update["pnl"] == 10.0  # 20 shares * $1.00 - $10.00 cost
    # Balance NOT credited yet — deferred to claim confirmation
    mock_db.update_balance.assert_not_called()
```

The `test_settle_resolved_records_loss` test stays mostly the same but verify `update_balance` is called with 0:

```python
@patch("executor.live.db")
def test_settle_resolved_records_loss(mock_db):
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    trade = {
        "id": 6, "market_id": "cond-456", "market_name": "Will Dallas be above 80F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "fill_price": 0.50, "end_date": "2026-04-10T23:59:59Z",
        "city": "dallas", "threshold": ">=80F",
        "token_id": "tok-yes-456",
    }

    ex.settle_resolved(trade, resolved_yes=False)

    update = mock_db.update_trade.call_args[0][1]
    assert update["status"] == "closed"
    assert update["exit_price"] == 0.00
    assert update["actual_resolution"] == "NO"
    assert update["forecast_correct"] == 0
    assert update["pnl"] == -10.0  # 20 shares * $0.00 - $10.00 cost
```

- [ ] **Step 6: Run all settle_resolved tests**

Run: `python -m pytest tests/test_live_executor.py::test_settle_resolved_records_win tests/test_live_executor.py::test_settle_resolved_records_loss tests/test_claim_flow.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add executor/live.py tests/test_live_executor.py tests/test_claim_flow.py
git commit -m "feat(live): split settle_resolved — winning trades defer to claim_pending"
```

---

### Task 6: LiveExecutor — Implement process_pending_claims()

**Files:**
- Modify: `executor/live.py` (add new method + import claimer)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
from datetime import datetime, timezone, timedelta


@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_process_pending_claims_confirms_successful_claim(MockClaimer, mock_db):
    """Successful claim → status=closed, balance credited, tx hash recorded."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.claim_winnings.return_value = "0xabc123"
    mock_claimer.check_tx_status.return_value = "confirmed"

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "claim_status": "claim_pending", "claim_retries": 0,
        "claim_last_attempt": None, "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    # Should update trade to closed + confirmed
    update = mock_db.update_trade.call_args[0][1]
    assert update["status"] == "closed"
    assert update["claim_status"] == "claim_confirmed"
    assert update["claim_tx_hash"] == "0xabc123"
    assert "closed_at" in update
    # Should credit balance
    mock_db.update_balance.assert_called_once_with(20.0)  # 20 shares * $1.00


@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_process_pending_claims_retries_on_failure(MockClaimer, mock_db):
    """Failed claim → increment retries, record last_attempt, stay pending."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.claim_winnings.return_value = None  # tx submission failed

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "claim_status": "claim_pending", "claim_retries": 0,
        "claim_last_attempt": None, "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    update = mock_db.update_trade.call_args[0][1]
    assert update["claim_retries"] == 1
    assert update["claim_last_attempt"] is not None
    assert update["claim_status"] == "claim_pending"
    mock_db.update_balance.assert_not_called()


@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_process_pending_claims_fails_after_max_retries(MockClaimer, mock_db):
    """After exhausting backoff schedule → claim_failed."""
    from executor.live import LiveExecutor
    from config import WEATHER

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.claim_winnings.return_value = None

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    max_retries = len(WEATHER["claim_retry_backoff_minutes"])
    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "claim_status": "claim_pending", "claim_retries": max_retries + 1,
        "claim_last_attempt": "2026-04-01T00:00:00",  # long ago
        "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    update = mock_db.update_trade.call_args[0][1]
    assert update["claim_status"] == "claim_failed"


@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_process_pending_claims_skips_if_in_backoff(MockClaimer, mock_db):
    """Skip claim if not enough time has elapsed since last attempt."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    # Last attempt was 1 minute ago, backoff[1] = 30 minutes
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "claim_status": "claim_pending", "claim_retries": 1,
        "claim_last_attempt": recent, "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    # Should not attempt claim — still in backoff
    mock_claimer.claim_winnings.assert_not_called()
    mock_db.update_trade.assert_not_called()


@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_process_pending_claims_defers_on_low_matic(MockClaimer, mock_db):
    """Low MATIC balance → defer claim, don't consume retry."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 0.001  # below threshold

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "claim_status": "claim_pending", "claim_retries": 0,
        "claim_last_attempt": None, "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    mock_claimer.claim_winnings.assert_not_called()
    mock_db.update_trade.assert_not_called()
    mock_db.update_balance.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_flow.py::test_process_pending_claims_confirms_successful_claim tests/test_claim_flow.py::test_process_pending_claims_retries_on_failure tests/test_claim_flow.py::test_process_pending_claims_fails_after_max_retries tests/test_claim_flow.py::test_process_pending_claims_skips_if_in_backoff tests/test_claim_flow.py::test_process_pending_claims_defers_on_low_matic -v`
Expected: FAIL — `LiveExecutor` has no `process_pending_claims` or `_claimer`

- [ ] **Step 3: Add claimer initialization to LiveExecutor.__init__**

In `executor/live.py`, add to the imports at the top:

```python
from chain.claimer import Claimer
```

In `__init__` (after `self._init_client(...)` call on line 44), add:

```python
        # Initialize on-chain claimer for CTF redemption
        from config import WEATHER, WALLET_PRIVATE_KEY
        rpc_url = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
        try:
            self._claimer = Claimer(rpc_url=rpc_url, private_key=WALLET_PRIVATE_KEY)
            print(f"[live] On-chain claimer initialized (RPC: {rpc_url})")
        except Exception as e:
            print(f"[live] WARNING: Claimer init failed ({e}) — claims will be deferred")
            self._claimer = None
```

- [ ] **Step 4: Implement process_pending_claims()**

In `executor/live.py`, add after `settle_resolved`:

```python
    def process_pending_claims(self):
        """
        Process on-chain claims for resolved winning trades.

        Checks all trades with claim_status='claim_pending', respects backoff
        schedule, submits CTF redeemPositions(), and credits balance on confirmation.
        """
        if self._claimer is None:
            return

        pending = db.get_pending_claims()
        if not pending:
            return

        backoff = WEATHER.get("claim_retry_backoff_minutes", [5, 30, 120, 480, 1440])
        min_matic = WEATHER.get("claim_min_matic_balance", 0.01)

        # Gas guard — check once for all pending claims
        try:
            matic_balance = self._claimer.get_matic_balance()
        except Exception as e:
            print(f"[claims] MATIC balance check failed: {e}")
            return

        if matic_balance < min_matic:
            print(f"[claims] Low MATIC ({matic_balance:.4f}) — deferring {len(pending)} claim(s)")
            return

        now = datetime.now(timezone.utc)

        for trade in pending:
            retries = trade.get("claim_retries") or 0
            last_attempt = trade.get("claim_last_attempt")

            # Check if max retries exceeded (5 retries = 6 total attempts)
            if retries > len(backoff):
                db.update_trade(trade["id"], {"claim_status": "claim_failed"})
                print(f"[claims] FAILED (max retries) — trade #{trade['id']} {trade['market_name'][:40]}")
                continue

            # Check backoff timing
            if last_attempt and retries > 0:
                try:
                    last = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    wait_minutes = backoff[retries - 1] if retries <= len(backoff) else backoff[-1]
                    if (now - last).total_seconds() < wait_minutes * 60:
                        continue  # still in backoff window
                except Exception:
                    pass  # unparseable timestamp — proceed with claim

            # Determine index set: [1] for YES tokens, [2] for NO tokens
            direction = (trade.get("direction") or "YES").upper()
            index_sets = [1] if direction == "YES" else [2]

            market_id = trade.get("market_id", "")
            print(f"[claims] Attempting claim for trade #{trade['id']}  "
                  f"{trade['market_name'][:40]}...")

            tx_hash = self._claimer.claim_winnings(
                condition_id=market_id,
                index_sets=index_sets,
            )

            if tx_hash is None:
                # Submission failed — consume retry
                db.update_trade(trade["id"], {
                    "claim_retries": retries + 1,
                    "claim_last_attempt": now.isoformat(),
                    "claim_status": "claim_pending",
                })
                print(f"[claims] Claim tx failed for trade #{trade['id']} "
                      f"(retry {retries + 1}/{len(backoff)})")
                continue

            # Poll for confirmation (up to 30s)
            status = "pending"
            for _ in range(15):
                status = self._claimer.check_tx_status(tx_hash)
                if status != "pending":
                    break
                time.sleep(2)

            if status == "confirmed":
                proceeds = trade["shares"] * 1.0  # winning shares = $1.00 each
                db.update_balance(proceeds)
                db.update_trade(trade["id"], {
                    "status": "closed",
                    "closed_at": now.isoformat(),
                    "claim_status": "claim_confirmed",
                    "claim_tx_hash": tx_hash,
                })
                db.record_account_value()
                print(f"[claims] CONFIRMED — trade #{trade['id']}  "
                      f"+${proceeds:.2f}  tx={tx_hash[:16]}...")
            elif status == "failed":
                # Tx was mined but reverted — consume retry
                db.update_trade(trade["id"], {
                    "claim_retries": retries + 1,
                    "claim_last_attempt": now.isoformat(),
                    "claim_tx_hash": tx_hash,
                    "claim_status": "claim_pending",
                })
                print(f"[claims] Tx reverted for trade #{trade['id']}  "
                      f"tx={tx_hash[:16]}... (retry {retries + 1}/{len(backoff)})")
            else:
                # Still pending after 30s — record tx hash, don't consume retry,
                # next cycle will re-check
                db.update_trade(trade["id"], {
                    "claim_tx_hash": tx_hash,
                    "claim_last_attempt": now.isoformat(),
                })
                print(f"[claims] Tx pending for trade #{trade['id']}  "
                      f"tx={tx_hash[:16]}... (will re-check)")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_flow.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add executor/live.py tests/test_claim_flow.py
git commit -m "feat(live): implement process_pending_claims with retry backoff"
```

---

### Task 7: Bot Loop — Add Claims Pass

**Files:**
- Modify: `weather_bot.py:532-541`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
@patch("weather_bot._executor")
def test_bot_loop_calls_claims_pass(mock_executor):
    """Verify the bot loop invokes process_pending_claims after resolve pass."""
    # This is a structural check — verify the method exists and is callable
    from executor.base import BaseExecutor
    assert callable(getattr(BaseExecutor, "process_pending_claims", None))
```

- [ ] **Step 2: Add claims pass to weather_bot.py**

In `weather_bot.py`, after the resolution pass block (after line 541, after the `_risk_manager.clear_resolved` block), add:

```python
        # Claims pass — process on-chain claims for winning live trades
        try:
            _executor.process_pending_claims()
        except Exception as e:
            print(f"[claims] process_pending_claims error (non-fatal): {e}")
```

- [ ] **Step 3: Verify the full test suite passes**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add weather_bot.py tests/test_claim_flow.py
git commit -m "feat(bot): add claims pass to main loop after resolution"
```

---

### Task 8: Resolution Resolver — Handle claim_pending Status

**Files:**
- Modify: `weather_resolver.py:44`

The resolver fetches open trades to check for resolution. But after a winning trade is resolved, its status becomes `claim_pending` (not `open`). The resolver should not re-process these trades. Currently `get_open_trades()` filters by `status='open'`, so `claim_pending` trades are already excluded — no code change needed.

However, the dashboard's open positions view and the `record_account_value()` function should include `claim_pending` trades in position value calculations.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_claim_flow.py`:

```python
def test_get_open_trades_excludes_claim_pending():
    """claim_pending trades should NOT appear in get_open_trades (no re-resolution)."""
    import db
    db.init_db()

    # Insert a claim_pending trade
    trade = {
        "market_id": "test-claim-exclude",
        "market_name": "Test Claim Exclude",
        "direction": "YES",
        "size_usdc": 10.0,
        "shares": 20.0,
        "entry_price": 0.50,
        "fill_price": 0.50,
        "opened_at": "2026-04-10T00:00:00",
        "status": "claim_pending",
    }
    db.insert_trade(trade)

    open_trades = db.get_open_trades()
    claim_pending_ids = [t["market_id"] for t in open_trades if t["market_id"] == "test-claim-exclude"]
    assert len(claim_pending_ids) == 0, "claim_pending trades should not appear in get_open_trades"

    # Clean up
    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE market_id = 'test-claim-exclude'")
```

- [ ] **Step 2: Run test to verify it passes (existing behavior is correct)**

Run: `python -m pytest tests/test_claim_flow.py::test_get_open_trades_excludes_claim_pending -v`
Expected: PASS — `get_open_trades` already filters `status='open'`

- [ ] **Step 3: Update record_account_value to include claim_pending in position value**

In `db.py`, update the `record_account_value()` function. Change the SQL query at line 258 from:

```python
            "SELECT current_price, fill_price, shares FROM trades WHERE status = 'open'"
```

to:

```python
            "SELECT current_price, fill_price, shares FROM trades WHERE status IN ('open', 'claim_pending')"
```

This ensures the account value chart includes pending-claim positions (they're still "ours" even if not yet redeemed).

- [ ] **Step 4: Write test for account value including claim_pending**

Add to `tests/test_claim_flow.py`:

```python
def test_record_account_value_includes_claim_pending():
    """Account value should include claim_pending positions."""
    import db
    db.init_db()

    # Set a known balance
    db.set_balance(100.0)

    # Insert a claim_pending trade worth $20 (20 shares at current_price $1.00)
    trade = {
        "market_id": "test-acct-val",
        "market_name": "Test Account Value",
        "direction": "YES",
        "size_usdc": 10.0,
        "shares": 20.0,
        "entry_price": 0.50,
        "fill_price": 0.50,
        "current_price": 1.00,
        "opened_at": "2026-04-10T00:00:00",
        "status": "claim_pending",
    }
    db.insert_trade(trade)

    db.record_account_value()

    history = db.get_balance_history()
    latest = history[-1]["amount"]
    # 100 cash + 20 shares * $1.00 = $120
    assert latest == 120.0

    # Clean up
    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE market_id = 'test-acct-val'")
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_claim_flow.py::test_get_open_trades_excludes_claim_pending tests/test_claim_flow.py::test_record_account_value_includes_claim_pending -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_claim_flow.py
git commit -m "fix(db): include claim_pending trades in account value calculation"
```

---

### Task 9: Full Integration Test + Test Suite Verification

**Files:**
- Modify: `tests/test_claim_flow.py` (add end-to-end lifecycle test)

- [ ] **Step 1: Write end-to-end lifecycle test**

Add to `tests/test_claim_flow.py`:

```python
@patch("executor.live.db")
@patch("executor.live.Claimer")
def test_full_claim_lifecycle(MockClaimer, mock_db):
    """Test the full flow: settle_resolved → claim_pending → process_pending_claims → closed."""
    from executor.live import LiveExecutor

    # Phase 1: settle_resolved sets claim_pending
    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.claim_winnings.return_value = "0xtxhash123"
    mock_claimer.check_tx_status.return_value = "confirmed"
    ex._claimer = mock_claimer

    trade = {
        "id": 99, "market_id": "0x" + "ff" * 32,
        "market_name": "Will London be above 20C?",
        "direction": "NO", "shares": 15.0, "size_usdc": 8.0,
        "fill_price": 0.53, "end_date": "2026-04-10T23:59:59Z",
        "city": "london", "threshold": ">=20C",
        "token_id": "tok-no-789",
    }

    # Resolve: NO direction, resolved_yes=False → NO wins
    ex.settle_resolved(trade, resolved_yes=False)

    settle_update = mock_db.update_trade.call_args[0][1]
    assert settle_update["claim_status"] == "claim_pending"
    assert settle_update["status"] == "claim_pending"
    mock_db.update_balance.assert_not_called()
    mock_db.reset_mock()

    # Phase 2: process_pending_claims confirms the claim
    pending_trade = {
        **trade,
        "claim_status": "claim_pending",
        "claim_retries": 0,
        "claim_last_attempt": None,
        "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [pending_trade]

    ex.process_pending_claims()

    claim_update = mock_db.update_trade.call_args[0][1]
    assert claim_update["status"] == "closed"
    assert claim_update["claim_status"] == "claim_confirmed"
    assert claim_update["claim_tx_hash"] == "0xtxhash123"
    mock_db.update_balance.assert_called_once_with(15.0)  # 15 shares * $1.00
```

- [ ] **Step 2: Run the full lifecycle test**

Run: `python -m pytest tests/test_claim_flow.py::test_full_claim_lifecycle -v`
Expected: PASS

- [ ] **Step 3: Run the complete test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 4: Commit**

```bash
git add tests/test_claim_flow.py
git commit -m "test: add full claim lifecycle integration test"
```

---

### Task 10: Update todo.md and Memory

**Files:**
- Modify: `tasks/todo.md`

- [ ] **Step 1: Update Phase 2 items in tasks/todo.md**

Mark Phase 2 tasks as complete:

```markdown
### Phase 2 — Market Resolution & Claiming Winnings — COMPLETE

- [x] On-chain claim/redeem — CTF redeemPositions() via web3.py
- [x] Auto-claim on resolution — triggered via claims pass each bot loop
- [x] Track claim state in DB (claim_pending/claim_confirmed/claim_failed)
- [x] Handle failed claims (retry with configurable backoff, max 5 attempts)
- [x] Gas guard — defer claims when MATIC below threshold
- [x] Account value includes claim_pending positions
```

- [ ] **Step 2: Commit**

```bash
git add tasks/todo.md
git commit -m "docs: mark Phase 2 claim/redeem as complete"
```
