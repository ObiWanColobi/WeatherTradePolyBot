# Phase 1: Live Executor + Wallet Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a fully functional live trading executor that can place, track, and close real orders on Polymarket via the CLOB API, with wallet balance sync, position reconciliation, and a paper/live config toggle.

**Architecture:** The existing `BaseExecutor` interface stays unchanged. A new `LiveExecutor` class implements all abstract methods using `py-clob-client` for order signing and submission. The bot selects `PaperExecutor` or `LiveExecutor` based on a single `TRADING_MODE` config flag. All decision logic, exit logic, sizing, and risk management remain untouched — only the execution layer changes.

**Tech Stack:** `py-clob-client` (Polymarket CLOB SDK), `web3` (allowance setup only), Python 3.11+, SQLite.

**Branch:** All work happens on a new `live` branch created from `main`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `executor/live.py` | **Create** | LiveExecutor — order signing, submission, status tracking, cancellation, fill confirmation, closing |
| `executor/base.py` | **Modify** | Add `place_extended_order` to abstract interface (currently only on PaperExecutor) |
| `config.py` | **Modify** | Add `TRADING_MODE`, live-specific defaults, wallet config |
| `weather_bot.py` | **Modify** | Executor selection based on `TRADING_MODE`, remove paper-only startup prompts for live mode |
| `db.py` | **Modify** | Add `order_id` and `fee_usdc` columns to trades table |
| `requirements.txt` | **Modify** | Uncomment `py-clob-client`, add `web3` |
| `scripts/setup_allowances.py` | **Create** | One-time script to approve CLOB contracts on Polygon |
| `tests/test_live_executor.py` | **Create** | Unit tests for LiveExecutor (mocked CLOB client) |
| `.env` | **Modify** | Document required keys for live mode |

---

## Task 1: Branch Setup + Dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Create the `live` branch**

```bash
git checkout main
git pull origin main
git checkout -b live
```

- [ ] **Step 2: Uncomment py-clob-client and add web3**

In `requirements.txt`, change:
```
# py-clob-client>=0.17.0  # uncomment when switching to live trading
```
to:
```
py-clob-client>=0.17.0
web3>=6.14.0
```

- [ ] **Step 3: Install dependencies**

```bash
pip install -r requirements.txt
```

Verify: `python -c "from py_clob_client.client import ClobClient; print('OK')"` should print `OK`.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: enable py-clob-client and web3 for live trading"
```

---

## Task 2: Config — TRADING_MODE + Live Defaults

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add TRADING_MODE and live wallet config to config.py**

Add after the `load_dotenv()` call at the top of `config.py`:

```python
# ── Trading Mode ─────────────────────────────────────────────────────────────
# "paper" = simulated fills using live order books (default)
# "live"  = real orders via py-clob-client (requires WALLET_PRIVATE_KEY in .env)
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# ── Live Trading Wallet ──────────────────────────────────────────────────────
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
# 0 = EOA (MetaMask/hardware), 1 = POLY_PROXY (Magic Link), 2 = GNOSIS_SAFE
WALLET_SIGNATURE_TYPE = int(os.getenv("WALLET_SIGNATURE_TYPE", "0"))
# Only needed for POLY_PROXY or GNOSIS_SAFE signature types
WALLET_FUNDER_ADDRESS = os.getenv("WALLET_FUNDER_ADDRESS", "")
```

- [ ] **Step 2: Add live-mode overrides to the WEATHER dict**

Add these keys inside the `WEATHER` dict, after the existing risk management section:

```python
    # ── Live trading overrides ───────────────────────────────────────────────
    # These values are used ONLY when TRADING_MODE == "live".
    # They override the paper defaults to be more conservative with real money.
    "live_kelly_max_bet_usdc":          25.00,    # start small — $25 max per trade
    "live_kelly_max_bet_usdc_unanimous": 10.00,   # $10 cap for unanimous-weak
    "live_risk_daily_loss_limit_pct":    0.05,    # 5% daily loss limit (vs 15% paper)
    "live_risk_auto_reset":              False,   # no auto-reset — manual override only
```

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add TRADING_MODE config and live wallet settings"
```

---

## Task 3: DB Schema — order_id + fee_usdc Columns

**Files:**
- Modify: `db.py`

- [ ] **Step 1: Add order_id and fee_usdc migration columns**

In `db.py`, find the block of `_safe_add_column` calls at the end of `init_db()` (after line ~190). Add these two lines at the end of that block:

```python
        _safe_add_column(conn, "trades", "order_id",  "TEXT")      # CLOB order ID (live trades only)
        _safe_add_column(conn, "trades", "fee_usdc",  "REAL")      # Polymarket fees deducted from fill
```

- [ ] **Step 2: Verify migration runs cleanly**

```bash
python -c "import db; db.init_db(); print('OK')"
```

Expected: `OK` with no errors.

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat: add order_id and fee_usdc columns to trades table"
```

---

## Task 4: Promote place_extended_order to BaseExecutor

**Files:**
- Modify: `executor/base.py`

- [ ] **Step 1: Add place_extended_order as an abstract method**

Currently `place_extended_order` only exists on `PaperExecutor`. Add it to `BaseExecutor` so `LiveExecutor` must implement it too. In `executor/base.py`, add after the `close_full` abstract method:

```python
    @abstractmethod
    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position."""

    @abstractmethod
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""

    @abstractmethod
    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """Settle a trade at market resolution."""
```

- [ ] **Step 2: Verify PaperExecutor still satisfies the interface**

```bash
python -c "from executor.paper import PaperExecutor; PaperExecutor(); print('OK')"
```

Expected: `OK` — PaperExecutor already implements all these methods.

- [ ] **Step 3: Commit**

```bash
git add executor/base.py
git commit -m "refactor: promote place_extended_order, close_position, settle_resolved to BaseExecutor"
```

---

## Task 5: LiveExecutor — Client Initialization + Balance Sync

**Files:**
- Create: `executor/live.py`

- [ ] **Step 1: Write the test for client initialization**

Create `tests/test_live_executor.py`:

```python
"""Tests for LiveExecutor — uses mocked ClobClient to avoid real API calls."""
import pytest
from unittest.mock import patch, MagicMock


def _make_mock_client(balance_wei="100000000", allowance_wei="999999999999"):
    """Create a mock ClobClient with configurable balance/allowance."""
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "balance": balance_wei,
        "allowance": allowance_wei,
    }
    return client


@patch("executor.live.ClobClient")
def test_init_creates_client_and_syncs_balance(MockClobClient):
    MockClobClient.return_value = _make_mock_client()
    MockClobClient.return_value.create_or_derive_api_creds.return_value = MagicMock()

    from executor.live import LiveExecutor
    ex = LiveExecutor.__new__(LiveExecutor)
    ex._init_client("fake_key", 137, 0, "")
    assert ex._client is not None


@patch("executor.live.ClobClient")
def test_get_exchange_balance(MockClobClient):
    mock = _make_mock_client(balance_wei="50000000")  # 50 USDC in 1e6 units
    MockClobClient.return_value = mock
    MockClobClient.return_value.create_or_derive_api_creds.return_value = MagicMock()

    from executor.live import LiveExecutor
    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = mock
    balance = ex._get_exchange_balance()
    assert balance == 50.0


@patch("executor.live.ClobClient")
def test_zero_allowance_raises(MockClobClient):
    mock = _make_mock_client(allowance_wei="0")
    MockClobClient.return_value = mock
    MockClobClient.return_value.create_or_derive_api_creds.return_value = MagicMock()

    from executor.live import LiveExecutor
    ex = LiveExecutor.__new__(LiveExecutor)
    with pytest.raises(RuntimeError, match="allowance"):
        ex._init_client("fake_key", 137, 0, "")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: FAIL — `executor/live.py` does not exist yet.

- [ ] **Step 3: Create executor/live.py with client init + balance sync**

Create `executor/live.py`:

```python
"""
Live Executor
──────────────
Real order execution on Polymarket via py-clob-client.
Implements the same BaseExecutor interface as PaperExecutor.
"""
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams, AssetType,
)

from executor.base import BaseExecutor
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS, WEATHER,
)
import markets.polymarket as polymarket
import db


# Polymarket uses USDC with 6 decimals on Polygon
_USDC_DECIMALS = 1_000_000


class LiveExecutor(BaseExecutor):
    """
    Executes real trades on Polymarket via the CLOB API.

    Fill prices come from actual order matching, not simulated book walks.
    Balance is synced from the exchange on startup and tracked locally
    after each trade (with periodic re-sync).
    """

    def __init__(self):
        self._client: ClobClient | None = None
        self._init_client(
            WALLET_PRIVATE_KEY,
            137,  # Polygon mainnet
            WALLET_SIGNATURE_TYPE,
            WALLET_FUNDER_ADDRESS,
        )

    def _init_client(self, private_key: str, chain_id: int,
                     signature_type: int, funder: str):
        """Initialize the CLOB client and verify allowance."""
        kwargs = {
            "host": POLYMARKET_CLOB_API,
            "key": private_key,
            "chain_id": chain_id,
            "signature_type": signature_type,
        }
        if funder:
            kwargs["funder"] = funder

        self._client = ClobClient(**kwargs)
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)

        # Verify allowance — if 0, orders will silently fail
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        allowance = int(bal_info.get("allowance", "0"))
        if allowance == 0:
            raise RuntimeError(
                "CLOB allowance is 0 — run scripts/setup_allowances.py first. "
                "Orders will fail without USDC approval on Polygon."
            )

        balance_usdc = int(bal_info.get("balance", "0")) / _USDC_DECIMALS
        print(f"[live] CLOB client initialized. Balance: ${balance_usdc:.2f} USDC")

    def _get_exchange_balance(self) -> float:
        """Fetch current USDC balance from the exchange."""
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return int(bal_info.get("balance", "0")) / _USDC_DECIMALS

    # ── Stub methods (implemented in subsequent tasks) ────────────────────────

    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict):
        raise NotImplementedError("Task 6")

    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        raise NotImplementedError("Task 7")

    def close_full(self, trade: dict, reason: str):
        raise NotImplementedError("Task 8")

    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        raise NotImplementedError("Task 8")

    def close_position(self, trade: dict, reason: str):
        raise NotImplementedError("Task 8")

    def settle_resolved(self, trade: dict, resolved_yes: bool):
        raise NotImplementedError("Task 9")

    def update_open_positions(self):
        raise NotImplementedError("Task 10")

    def reconcile_positions(self):
        raise NotImplementedError("Task 11")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add executor/live.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor scaffold with client init and balance sync"
```

---

## Task 6: LiveExecutor — place_order (Entry Trades)

**Files:**
- Modify: `executor/live.py`
- Modify: `tests/test_live_executor.py`

- [ ] **Step 1: Write the test for place_order**

Add to `tests/test_live_executor.py`:

```python
@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_place_order_posts_to_clob(mock_pm, mock_db):
    from executor.live import LiveExecutor
    from unittest.mock import PropertyMock

    mock_db.get_balance.return_value = 100.0
    mock_db.get_open_trade_for_market.return_value = None

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_and_post_order.return_value = {
        "orderID": "test-order-123",
        "status": "matched",
    }
    ex._client.get_order.return_value = {
        "id": "test-order-123",
        "status": "matched",
        "price": "0.45",
        "size_matched": "20.0",
        "associate_trades": [
            {"price": "0.45", "size": "20.0", "fee": "0.18"}
        ],
    }

    market = {
        "id": "cond-123",
        "question": "Will NYC be above 60F?",
        "token_id": "tok-yes-123",
        "no_token_id": "tok-no-123",
        "price": 0.50,
        "end_date": "2026-04-15T23:59:59Z",
        "city": "new york city",
        "market_url": "https://polymarket.com/event/test",
        "liquidity": 5000,
        "volume": 10000,
    }
    estimate = {
        "probability": 0.75,
        "edge_score": 25.0,
        "sources": ["weather_forecast"],
        "entry_ensemble_pct": 0.90,
        "entry_ensemble_yes": 62,
        "entry_ensemble_n": 69,
        "threshold": ">=60F",
    }

    ex.place_order(market, "YES", 10.0, estimate)

    ex._client.create_and_post_order.assert_called_once()
    mock_db.insert_trade.assert_called_once()
    trade_arg = mock_db.insert_trade.call_args[0][0]
    assert trade_arg["order_id"] == "test-order-123"
    assert trade_arg["direction"] == "YES"
    assert trade_arg["status"] == "open"


@patch("executor.live.db")
def test_place_order_skips_insufficient_balance(mock_db):
    from executor.live import LiveExecutor

    mock_db.get_balance.return_value = 5.0

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()

    ex.place_order({"id": "x", "token_id": "t"}, "YES", 10.0, {})

    ex._client.create_and_post_order.assert_not_called()
    mock_db.insert_trade.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_live_executor.py::test_place_order_posts_to_clob -v
```

Expected: FAIL — `place_order` raises `NotImplementedError`.

- [ ] **Step 3: Implement place_order**

Replace the `place_order` stub in `executor/live.py` with:

```python
    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict):
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[live] Skipping — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        if db.get_open_trade_for_market(market["id"]):
            return

        # Select token — YES buys YES token, NO buys NO token
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return

        # Get current best ask to set limit price
        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Skipping — no midpoint for {market['question'][:50]}")
            return

        # Place a limit order at slightly above mid to ensure fill
        # (aggressive limit = mid + 1 tick, acts like a market order with price protection)
        limit_price = round(min(mid + 0.01, 0.99), 2)
        shares = size_usdc / limit_price

        order_response = self._post_order(token_id, limit_price, shares, "BUY")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")

        # Poll for fill confirmation
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, limit_price)
        if filled_shares <= 0:
            print(f"[live] Order {order_id[:12]} not filled — cancelling")
            self._cancel_order(order_id)
            return

        filled_usdc = filled_shares * fill_price

        # Calculate display values
        yes_price = market.get("price", 0.5)
        model_prob = estimate.get("probability", 0)
        if direction == "YES":
            token_mid = yes_price
            edge_display = model_prob - yes_price
        else:
            token_mid = 1.0 - yes_price
            edge_display = yes_price - model_prob
        slippage = abs(fill_price - token_mid)

        trade = {
            "market_id":       market["id"],
            "market_name":     market.get("question", ""),
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          filled_shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.now(timezone.utc).isoformat(),
            "hours_to_close_at_entry": _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":       market.get("liquidity"),
            "volume_24h":      market.get("volume"),
            "city":            market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":      market.get("market_url", ""),
            "threshold":       estimate.get("threshold"),
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "order_id":        order_id,
            "fee_usdc":        fee,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        print(f"[live] OPEN  {direction:3s}  {market.get('question', '')[:55]}")
        print(f"             Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Edge: {edge_display:+.3f}  Fee: ${fee:.2f}")
```

- [ ] **Step 4: Add the helper methods _post_order, _confirm_fill, _cancel_order**

Add these private methods to the `LiveExecutor` class:

```python
    def _post_order(self, token_id: str, price: float, size: float,
                    side: str) -> dict | None:
        """Sign and post an order to the CLOB. Returns response dict or None."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_side = BUY if side == "BUY" else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )

        try:
            signed = self._client.create_order(order_args)
            response = self._client.post_order(signed, OrderType.FOK)
            print(f"[live] Order posted: {response.get('orderID', '?')[:12]}  "
                  f"status={response.get('status', '?')}")
            return response
        except Exception as e:
            print(f"[live] Order post failed: {e}")
            return None

    def _confirm_fill(self, order_id: str, token_id: str,
                      expected_price: float) -> tuple[float, float, float]:
        """
        Poll order status to confirm fill.

        Returns:
            (fill_price, filled_shares, total_fee)
        """
        import time

        for attempt in range(5):
            try:
                order = self._client.get_order(order_id)
                status = order.get("status", "")

                if status in ("matched", "filled"):
                    trades = order.get("associate_trades", [])
                    if trades:
                        total_cost = 0.0
                        total_shares = 0.0
                        total_fee = 0.0
                        for t in trades:
                            p = float(t.get("price", 0))
                            s = float(t.get("size", 0))
                            f = float(t.get("fee", 0))
                            total_cost += p * s
                            total_shares += s
                            total_fee += f
                        avg_price = total_cost / total_shares if total_shares > 0 else expected_price
                        return avg_price, total_shares, total_fee

                    # Matched but no trade details yet — use order-level fields
                    matched = float(order.get("size_matched", 0))
                    if matched > 0:
                        price = float(order.get("price", expected_price))
                        return price, matched, 0.0

                if status in ("cancelled", "expired", "dead"):
                    return 0.0, 0.0, 0.0

            except Exception as e:
                print(f"[live] Fill check attempt {attempt + 1} failed: {e}")

            if attempt < 4:
                time.sleep(2)

        # Timeout — treat as unfilled
        return 0.0, 0.0, 0.0

    def _cancel_order(self, order_id: str):
        """Cancel an open order. Best-effort — logs but doesn't raise."""
        try:
            self._client.cancel(order_id)
            print(f"[live] Cancelled order {order_id[:12]}")
        except Exception as e:
            print(f"[live] Cancel failed for {order_id[:12]}: {e}")
```

- [ ] **Step 5: Add the _hours_until helper at module level**

Add at the bottom of `executor/live.py`:

```python
def _hours_until(end_date_str: str | None) -> float | None:
    """Hours from now until end_date_str. Returns None if unparseable or missing."""
    if not end_date_str:
        return None
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return (end - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add executor/live.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor.place_order — real CLOB order signing and submission"
```

---

## Task 7: LiveExecutor — place_extended_order

**Files:**
- Modify: `executor/live.py`
- Modify: `tests/test_live_executor.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_live_executor.py`:

```python
@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_place_extended_order_posts_to_clob(mock_pm, mock_db):
    from executor.live import LiveExecutor

    mock_db.get_balance.return_value = 100.0
    mock_pm.get_midpoint.return_value = 0.40

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_and_post_order.return_value = {"orderID": "ext-456", "status": "matched"}
    ex._client.get_order.return_value = {
        "id": "ext-456", "status": "matched",
        "associate_trades": [{"price": "0.41", "size": "12.0", "fee": "0.10"}],
    }

    market = {
        "id": "cond-123", "question": "Will NYC be above 60F?",
        "token_id": "tok-yes-123", "price": 0.50,
        "end_date": "2026-04-15T23:59:59Z", "city": "new york city",
    }
    estimate = {
        "probability": 0.75, "edge_score": 25.0,
        "sources": ["weather_forecast"],
        "entry_ensemble_pct": 0.90, "entry_ensemble_yes": 62, "entry_ensemble_n": 69,
        "threshold": ">=60F",
    }

    ex.place_extended_order(market, "YES", 10.0, estimate, parent_trade_id=1, leg_number=2)

    mock_db.insert_trade.assert_called_once()
    trade_arg = mock_db.insert_trade.call_args[0][0]
    assert trade_arg["parent_trade_id"] == 1
    assert trade_arg["leg_number"] == 2
    assert trade_arg["order_id"] == "ext-456"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_live_executor.py::test_place_extended_order_posts_to_clob -v
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement place_extended_order**

Replace the `place_extended_order` stub in `executor/live.py`:

```python
    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position via CLOB."""
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[live] Skipping add-on — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        token_id = market.get("token_id")
        if not token_id:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Skipping add-on — no midpoint")
            return

        # Slippage check before ordering
        max_slippage = WEATHER.get("entry_max_slippage_pct", 0.05)
        limit_price = round(min(mid + 0.01, 0.99), 2)

        # Try full size, then reduce if slippage concern
        for frac in (1.0, 0.75, 0.50, 0.25):
            attempt_usdc = size_usdc * frac
            if attempt_usdc < 1.0:
                break
            attempt_shares = attempt_usdc / limit_price
            slippage_pct = abs(limit_price - mid) / mid if mid > 0 else 0
            if slippage_pct <= max_slippage:
                size_usdc = attempt_usdc
                shares = attempt_shares
                break
        else:
            print(f"[live] Skipping add-on — slippage too high even at 25% size")
            return

        order_response = self._post_order(token_id, limit_price, shares, "BUY")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, limit_price)

        if filled_shares <= 0:
            print(f"[live] Add-on order {order_id[:12]} not filled — cancelling")
            self._cancel_order(order_id)
            return

        filled_usdc = filled_shares * fill_price
        yes_price = market.get("price", 0.5)
        token_mid = yes_price if direction == "YES" else 1.0 - yes_price
        slippage = abs(fill_price - token_mid)

        trade = {
            "market_id":       market["id"],
            "market_name":     market.get("question", ""),
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          filled_shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.now(timezone.utc).isoformat(),
            "hours_to_close_at_entry": _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":       market.get("liquidity"),
            "volume_24h":      market.get("volume"),
            "city":            market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":      market.get("market_url", ""),
            "threshold":       estimate.get("threshold"),
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "parent_trade_id": parent_trade_id,
            "leg_number":      leg_number,
            "order_id":        order_id,
            "fee_usdc":        fee,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        print(f"[live] ADD-ON Leg {leg_number}  {direction:3s}  {market.get('question', '')[:50]}")
        print(f"             Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Fee: ${fee:.2f}")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add executor/live.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor.place_extended_order — add-on legs via CLOB"
```

---

## Task 8: LiveExecutor — close_full, close_partial, close_position

**Files:**
- Modify: `executor/live.py`
- Modify: `tests/test_live_executor.py`

- [ ] **Step 1: Write the test for close_full**

Add to `tests/test_live_executor.py`:

```python
@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_close_full_sells_shares(mock_pm, mock_db):
    from executor.live import LiveExecutor

    mock_pm.get_midpoint.return_value = 0.60

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_and_post_order.return_value = {"orderID": "sell-789", "status": "matched"}
    ex._client.get_order.return_value = {
        "id": "sell-789", "status": "matched",
        "associate_trades": [{"price": "0.59", "size": "20.0", "fee": "0.12"}],
    }

    trade = {
        "id": 1, "market_id": "cond-123", "market_name": "Will NYC be above 60F?",
        "token_id": "tok-yes-123", "direction": "YES",
        "shares": 20.0, "size_usdc": 10.0,
        "fill_price": 0.50, "current_price": 0.60,
        "end_date": "2026-04-15T23:59:59Z", "exit_reason": None,
    }

    ex.close_full(trade, reason="ensemble_flip")

    ex._client.create_order.assert_called_once()
    mock_db.update_trade.assert_called_once()
    update_args = mock_db.update_trade.call_args[0]
    assert update_args[0] == 1  # trade id
    assert update_args[1]["status"] == "closed"
    assert update_args[1]["exit_reason"] == "ensemble_flip"
    mock_db.update_balance.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_live_executor.py::test_close_full_sells_shares -v
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement close_full, close_partial, close_position**

Replace the three stubs in `executor/live.py`:

```python
    def close_full(self, trade: dict, reason: str):
        """Sell all shares of a position on the CLOB."""
        token_id = trade.get("token_id")
        shares = trade.get("shares", 0)
        if not token_id or shares <= 0:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Cannot close — no midpoint for {trade['market_name'][:50]}")
            return

        # Aggressive sell — limit price slightly below mid for fast fill
        sell_price = round(max(mid - 0.01, 0.01), 2)

        order_response = self._post_order(token_id, sell_price, shares, "SELL")
        if order_response is None:
            # Fallback: try at 1 cent (effectively market sell)
            print(f"[live] Retrying close at $0.01 floor...")
            order_response = self._post_order(token_id, 0.01, shares, "SELL")
            if order_response is None:
                print(f"[live] FAILED to close {trade['market_name'][:50]} — manual intervention needed")
                return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, sell_price)

        if filled_shares <= 0:
            print(f"[live] Close order not filled — {trade['market_name'][:50]}")
            self._cancel_order(order_id)
            return

        exit_price = fill_price
        proceeds = filled_shares * exit_price
        cost = trade["size_usdc"]
        pnl = proceeds - cost - fee
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":             exit_price,
            "closed_at":              datetime.now(timezone.utc).isoformat(),
            "status":                 "closed",
            "pnl":                    pnl,
            "pnl_pct":               pnl_pct,
            "exit_reason":           _append_reason(trade.get("exit_reason"), reason),
            "hours_to_close_at_exit": _hours_until(trade.get("end_date")),
            "order_id":              order_id,
            "fee_usdc":              (trade.get("fee_usdc") or 0) + fee,
        })
        db.record_account_value()

        print(f"[live] CLOSE {trade['market_name'][:55]}")
        print(f"             Reason: {reason}  P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)  Fee: ${fee:.2f}")

    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        """Sell a fraction of shares. If remainder is tiny, close fully."""
        shares_to_sell = trade["shares"] * sell_pct
        if shares_to_sell < 0.01:
            return

        remaining_shares = trade["shares"] - shares_to_sell
        if remaining_shares < 0.01:
            self.close_full(trade, reason)
            return

        token_id = trade.get("token_id")
        if not token_id:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            return

        sell_price = round(max(mid - 0.01, 0.01), 2)
        order_response = self._post_order(token_id, sell_price, shares_to_sell, "SELL")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, sell_price)
        if filled_shares <= 0:
            self._cancel_order(order_id)
            return

        proceeds = filled_shares * fill_price
        partial_pnl = proceeds - (filled_shares * trade["fill_price"]) - fee

        db.update_balance(proceeds)
        db.record_account_value()

        actual_remaining = trade["shares"] - filled_shares
        remaining_usdc = actual_remaining * trade["fill_price"]

        db.update_trade(trade["id"], {
            "shares":          actual_remaining,
            "size_usdc":       remaining_usdc,
            "bleed_rungs_hit": trade.get("bleed_rungs_hit", 0) + 1,
            "exit_reason":     _append_reason(trade.get("exit_reason"), reason),
            "fee_usdc":        (trade.get("fee_usdc") or 0) + fee,
        })

        print(f"[live] PARTIAL ({sell_pct*100:.0f}%)  {trade['market_name'][:50]}")
        print(f"             Reason: {reason}  Proceeds: ${proceeds:.2f}  "
              f"Partial P&L: ${partial_pnl:+.2f}  Fee: ${fee:.2f}")

    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)
```

- [ ] **Step 4: Add _append_reason helper at module level**

Add at the bottom of `executor/live.py` (after `_hours_until`):

```python
def _append_reason(existing: str | None, new: str) -> str:
    if existing:
        return f"{existing} → {new}"
    return new
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add executor/live.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor close_full, close_partial, close_position — CLOB sell orders"
```

---

## Task 9: LiveExecutor — settle_resolved

**Files:**
- Modify: `executor/live.py`
- Modify: `tests/test_live_executor.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_live_executor.py`:

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
    assert update["status"] == "closed"
    assert update["exit_price"] == 1.00
    assert update["actual_resolution"] == "YES"
    assert update["forecast_correct"] == 1
    assert update["pnl"] == 10.0  # 20 shares * $1.00 - $10.00 cost


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
    assert update["exit_price"] == 0.00
    assert update["actual_resolution"] == "NO"
    assert update["forecast_correct"] == 0
    assert update["pnl"] == -10.0  # 20 shares * $0.00 - $10.00 cost
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_live_executor.py::test_settle_resolved_records_win -v
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement settle_resolved**

Replace the stub in `executor/live.py`. This mirrors PaperExecutor's logic — resolution settlement is the same for live (the actual on-chain claim/redeem is Phase 2 work):

```python
    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """
        Settle a trade at market resolution.

        On-chain claiming of winnings is Phase 2 work. For now, this records
        the resolution in the DB and updates the balance. Winning shares
        pay $1.00 each, losing shares pay $0.00.
        """
        direction = trade.get("direction", "YES").upper()
        won = (direction == "YES" and resolved_yes) or \
              (direction == "NO" and not resolved_yes)
        actual = "YES" if resolved_yes else "NO"
        close_price = 1.0 if resolved_yes else 0.0

        exit_price = 1.00 if won else 0.00
        proceeds = trade["shares"] * exit_price

        cost = trade["size_usdc"]
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":              exit_price,
            "closed_at":               datetime.now(timezone.utc).isoformat(),
            "status":                  "closed",
            "pnl":                     pnl,
            "pnl_pct":                 pnl_pct,
            "exit_reason":             "resolved",
            "actual_resolution":       actual,
            "forecast_correct":        1 if won else 0,
            "resolution_price":        close_price,
            "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
        })
        db.record_account_value()

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

        outcome = "WIN" if won else "LOSS"
        print(f"  [resolve] {outcome}  {trade['market_name'][:52]}")
        print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add executor/live.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor.settle_resolved — resolution settlement with P&L"
```

---

## Task 10: LiveExecutor — update_open_positions

**Files:**
- Modify: `executor/live.py`

- [ ] **Step 1: Implement update_open_positions**

This method is identical to PaperExecutor's — it refreshes prices via CLOB midpoint. Replace the stub:

```python
    def update_open_positions(self):
        """
        Refresh current_price, peak_price, and liquidity for every open position.
        Uses CLOB midpoint for price, Gamma API for liquidity/volume.
        """
        open_trades = db.get_open_trades()
        for trade in open_trades:
            new_price = self._fetch_current_price(trade)
            if new_price is None:
                continue

            peak = max(trade.get("peak_price") or 0.0, new_price)
            updates = {"current_price": new_price, "peak_price": peak}

            market = polymarket.get_market_by_id(trade["market_id"])
            if market:
                liq = market.get("liquidity") or 0
                if liq > 0:
                    updates["liquidity"] = liq
                vol = market.get("volume") or 0
                if vol > 0:
                    updates["volume_24h"] = vol
                if not trade.get("market_url") and market.get("market_url"):
                    updates["market_url"] = market["market_url"]

            db.update_trade(trade["id"], updates)

    def _fetch_current_price(self, trade: dict) -> float | None:
        """Fetch the current token price via CLOB midpoint, with Gamma fallback."""
        token_id = trade.get("token_id")
        if token_id:
            price = polymarket.get_midpoint(token_id)
            if price and 0 < price < 1:
                return price
        market = polymarket.get_market_by_id(trade["market_id"])
        if market:
            yes_price = market.get("price")
            if yes_price is not None and 0 < yes_price < 1:
                direction = trade.get("direction", "YES").upper()
                return yes_price if direction == "YES" else (1.0 - yes_price)
        return None
```

- [ ] **Step 2: Verify compilation**

```bash
python -c "from executor.live import LiveExecutor; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add executor/live.py
git commit -m "feat: LiveExecutor.update_open_positions — CLOB midpoint price refresh"
```

---

## Task 11: LiveExecutor — reconcile_positions

**Files:**
- Modify: `executor/live.py`
- Modify: `tests/test_live_executor.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_live_executor.py`:

```python
@patch("executor.live.db")
def test_reconcile_syncs_balance_from_exchange(mock_db):
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.get_balance_allowance.return_value = {
        "balance": "75000000", "allowance": "999999999"  # 75 USDC
    }

    mock_db.get_open_trades.return_value = []

    ex.reconcile_positions()

    # Should sync exchange balance to DB
    mock_db.set_balance.assert_called_once_with(75.0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_live_executor.py::test_reconcile_syncs_balance_from_exchange -v
```

Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Add db.set_balance to db.py**

In `db.py`, find the `update_balance` function. Add a new function directly after it:

```python
def set_balance(amount: float):
    """Set the balance to an exact value (used for exchange sync)."""
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE balance SET amount = ?, updated_at = ? WHERE id = 1",
            (amount, now),
        )
```

- [ ] **Step 4: Implement reconcile_positions**

Replace the stub in `executor/live.py`:

```python
    def reconcile_positions(self):
        """
        On restart, sync with exchange state:
        1. Update DB balance to match exchange USDC balance
        2. Log open positions being resumed
        3. Warn about any discrepancies
        """
        # Sync balance from exchange
        exchange_balance = self._get_exchange_balance()
        db_balance = db.get_balance()

        if abs(exchange_balance - db_balance) > 0.01:
            print(f"[live] Balance sync: DB=${db_balance:.2f} → Exchange=${exchange_balance:.2f}")
            db.set_balance(exchange_balance)
        else:
            print(f"[live] Balance in sync: ${exchange_balance:.2f}")

        # Log open positions
        open_trades = db.get_open_trades()
        if not open_trades:
            print("[live] No open positions to resume.")
            return

        print(f"[live] Resuming {len(open_trades)} open position(s):")
        for t in open_trades:
            print(f"       {t['direction']:3s}  {t['market_name'][:60]}  "
                  f"fill={t['fill_price']:.4f}  size=${t['size_usdc']:.2f}")
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_live_executor.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add executor/live.py db.py tests/test_live_executor.py
git commit -m "feat: LiveExecutor.reconcile_positions — exchange balance sync on restart"
```

---

## Task 12: Bot Loop — Executor Selection + Live Mode Startup

**Files:**
- Modify: `weather_bot.py`

- [ ] **Step 1: Add executor selection based on TRADING_MODE**

In `weather_bot.py`, replace the current executor import and initialization (lines 26, 40):

Change:
```python
from executor.paper import PaperExecutor
```
to:
```python
from config import TRADING_MODE
```

And replace:
```python
_executor     = PaperExecutor()
```
with:
```python
def _create_executor():
    if TRADING_MODE == "live":
        from executor.live import LiveExecutor
        return LiveExecutor()
    else:
        from executor.paper import PaperExecutor
        return PaperExecutor()

_executor = _create_executor()
```

- [ ] **Step 2: Update the run() function startup banner**

In the `run()` function, change:
```python
    print("[bot] Weather Trading Bot — paper mode")
```
to:
```python
    mode_label = "LIVE" if TRADING_MODE == "live" else "paper"
    print(f"[bot] Weather Trading Bot — {mode_label} mode")
```

- [ ] **Step 3: Skip the paper-only reset prompt in live mode**

In the `run()` function, change:
```python
    _prompt_startup()
```
to:
```python
    if TRADING_MODE == "live":
        print("[bot] Live mode — skipping session reset prompt.")
    else:
        _prompt_startup()
```

- [ ] **Step 4: Apply live config overrides**

In the `run()` function, after `_risk_manager = RiskManager()`, add:

```python
    # Apply live-mode config overrides
    if TRADING_MODE == "live":
        WEATHER["kelly_max_bet_usdc"] = WEATHER.get("live_kelly_max_bet_usdc", 25.0)
        WEATHER["kelly_max_bet_usdc_unanimous"] = WEATHER.get("live_kelly_max_bet_usdc_unanimous", 10.0)
        WEATHER["risk_daily_loss_limit_pct"] = WEATHER.get("live_risk_daily_loss_limit_pct", 0.05)
        WEATHER["risk_auto_reset"] = WEATHER.get("live_risk_auto_reset", False)
```

- [ ] **Step 5: Add reconcile_positions call on startup**

In the `run()` function, after the calibration startup block, add:

```python
    # Reconcile positions with exchange on startup
    _executor.reconcile_positions()
```

- [ ] **Step 6: Update argument parser description**

Change:
```python
    parser = argparse.ArgumentParser(description="Weather trading bot (paper mode)")
```
to:
```python
    parser = argparse.ArgumentParser(description="Weather trading bot")
```

- [ ] **Step 7: Verify paper mode still works**

```bash
python -c "from config import TRADING_MODE; print(f'Mode: {TRADING_MODE}')"
```

Expected: `Mode: paper` (default when `TRADING_MODE` is not set in `.env`).

- [ ] **Step 8: Commit**

```bash
git add weather_bot.py
git commit -m "feat: bot loop selects LiveExecutor or PaperExecutor based on TRADING_MODE"
```

---

## Task 13: Allowance Setup Script

**Files:**
- Create: `scripts/setup_allowances.py`

- [ ] **Step 1: Create the scripts directory**

```bash
mkdir -p scripts
```

- [ ] **Step 2: Create the setup script**

Create `scripts/setup_allowances.py`:

```python
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
```

- [ ] **Step 3: Commit**

```bash
git add scripts/setup_allowances.py
git commit -m "feat: one-time allowance setup script for Polymarket CLOB contracts"
```

---

## Task 14: Deploy Script for Live Branch

**Files:**
- Create: `deploy_live.sh`

- [ ] **Step 1: Create deploy_live.sh**

```bash
#!/bin/bash
# Deploy script for PythonAnywhere — LIVE branch
# Run from a PythonAnywhere Bash console
# Usage: bash deploy_live.sh
#
# IMPORTANT: This deploys the 'live' branch with real money trading.
# Make sure TRADING_MODE=live is set in .env on the server.

set -e

REPO_DIR="/home/obiwancolobi/WeatherTradePolyBot-Live"
PA_USERNAME="obiwancolobi"
PA_API="https://www.pythonanywhere.com/api/v0/user/$PA_USERNAME"

echo "=== LIVE DEPLOYMENT ==="
echo "WARNING: This deploys real-money trading code."
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo "=== Pulling latest changes from live branch ==="
cd "$REPO_DIR"
git pull origin live

echo "=== Installing/updating dependencies ==="
pip install -r requirements.txt --quiet

echo "=== Restarting always-on task ==="
if [ -z "$PA_API_TOKEN" ]; then
    echo "WARNING: PA_API_TOKEN not set. Skipping auto-restart."
    echo "  -> Manually restart in the PythonAnywhere Tasks dashboard."
else
    TASK_ID=$(curl -s -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
for t in tasks:
    if 'weather_bot.py' in t.get('command', '') and 'Live' in t.get('description', ''):
        print(t['id'])
        break
")

    if [ -z "$TASK_ID" ]; then
        echo "ERROR: Could not find live always-on task."
        echo "  Create one first in PythonAnywhere with description containing 'Live'."
        exit 1
    fi

    curl -s -X POST -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/$TASK_ID/restart/" > /dev/null

    echo "Always-on task $TASK_ID restarted."
fi

echo "=== Live deploy complete ==="
```

- [ ] **Step 2: Commit**

```bash
git add deploy_live.sh
git commit -m "feat: deployment script for live trading branch"
```

---

## Task 15: Integration Verification

**Files:** None new — verification only.

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: All tests PASS.

- [ ] **Step 2: Verify paper mode is unaffected**

```bash
python -c "
import os
os.environ['TRADING_MODE'] = 'paper'
from config import TRADING_MODE
from executor.paper import PaperExecutor
ex = PaperExecutor()
print(f'Mode: {TRADING_MODE}')
print(f'Executor: {type(ex).__name__}')
print('Paper mode OK')
"
```

Expected:
```
Mode: paper
Executor: PaperExecutor
Paper mode OK
```

- [ ] **Step 3: Verify live executor imports cleanly**

```bash
python -c "
from executor.live import LiveExecutor
print('LiveExecutor imports OK')
"
```

Expected: `LiveExecutor imports OK` (may print warnings about missing env vars — that's fine).

- [ ] **Step 4: Verify DB migration runs cleanly**

```bash
python -c "
import db
db.init_db()
conn = db.get_conn()
cols = [row[1] for row in conn.execute('PRAGMA table_info(trades)').fetchall()]
assert 'order_id' in cols, 'Missing order_id column'
assert 'fee_usdc' in cols, 'Missing fee_usdc column'
print(f'Trades table has {len(cols)} columns — order_id and fee_usdc present')
"
```

Expected: Confirmation message with column counts.

- [ ] **Step 5: Commit any fixes, then push the live branch**

```bash
git push -u origin live
```

---

## Summary of Deliverables

| What | Where |
|------|-------|
| Live executor (full CLOB integration) | `executor/live.py` |
| Base executor interface (updated) | `executor/base.py` |
| Config with TRADING_MODE toggle | `config.py` |
| DB schema (order_id, fee_usdc) | `db.py` |
| Bot loop with mode selection | `weather_bot.py` |
| Allowance setup script | `scripts/setup_allowances.py` |
| Live deploy script | `deploy_live.sh` |
| Unit tests | `tests/test_live_executor.py` |
| Dependencies | `requirements.txt` |

**What this does NOT include (deferred to Phase 2+):**
- On-chain claiming of resolved winnings (Phase 2)
- Emergency kill switch (Phase 3)
- Health heartbeat / crash detection (Phase 4)
- Alert notifications (Phase 4)
