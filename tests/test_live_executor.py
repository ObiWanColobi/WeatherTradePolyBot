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


# ── Client init + balance sync ───────────────────────────────────────────────

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


# ── place_order ──────────────────────────────────────────────────────────────

@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_place_order_posts_to_clob(mock_pm, mock_db):
    from executor.live import LiveExecutor

    mock_db.get_balance.return_value = 100.0
    mock_db.get_open_trade_for_market.return_value = None
    mock_pm.get_midpoint.return_value = 0.45

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_order.return_value = MagicMock()
    ex._client.post_order.return_value = {
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

    ex._client.create_order.assert_called_once()
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

    ex._client.create_order.assert_not_called()
    mock_db.insert_trade.assert_not_called()


# ── place_extended_order ─────────────────────────────────────────────────────

@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_place_extended_order_posts_to_clob(mock_pm, mock_db):
    from executor.live import LiveExecutor

    mock_db.get_balance.return_value = 100.0
    mock_pm.get_midpoint.return_value = 0.40

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_order.return_value = MagicMock()
    ex._client.post_order.return_value = {"orderID": "ext-456", "status": "matched"}
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


# ── close_full ───────────────────────────────────────────────────────────────

@patch("executor.live.db")
@patch("executor.live.polymarket")
def test_close_full_sells_shares(mock_pm, mock_db):
    from executor.live import LiveExecutor

    mock_pm.get_midpoint.return_value = 0.60

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.create_order.return_value = MagicMock()
    ex._client.post_order.return_value = {"orderID": "sell-789", "status": "matched"}
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
        "fee_usdc": 0.0,
    }

    ex.close_full(trade, reason="ensemble_flip")

    ex._client.create_order.assert_called_once()
    mock_db.update_trade.assert_called_once()
    update_args = mock_db.update_trade.call_args[0]
    assert update_args[0] == 1  # trade id
    assert update_args[1]["status"] == "closed"
    assert update_args[1]["exit_reason"] == "ensemble_flip"
    mock_db.update_balance.assert_called_once()


# ── settle_resolved ──────────────────────────────────────────────────────────

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


# ── reconcile_positions ──────────────────────────────────────────────────────

@patch("executor.live.db")
def test_reconcile_syncs_balance_from_exchange(mock_db):
    from executor.live import LiveExecutor

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._client.get_balance_allowance.return_value = {
        "balance": "75000000", "allowance": "999999999"  # 75 USDC
    }

    mock_db.get_open_trades.return_value = []
    mock_db.get_balance.return_value = 50.0  # DB says 50, exchange says 75

    ex.reconcile_positions()

    mock_db.set_balance.assert_called_once_with(75.0)
