"""Tests for the on-chain claim lifecycle."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


def test_claim_config_keys_exist():
    """Verify claim config keys are present in WEATHER dict."""
    from config import WEATHER
    assert "claim_retry_backoff_minutes" in WEATHER
    assert "claim_min_matic_balance" in WEATHER
    assert "polygon_rpc_url" in WEATHER
    assert len(WEATHER["claim_retry_backoff_minutes"]) == 5
    assert WEATHER["claim_retry_backoff_minutes"] == [5, 30, 120, 480, 1440]


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


# ── settle_resolved — winning vs losing ─────────────────────────────────────


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


# ── process_pending_claims ──────────────────────────────────────────────────


@patch("executor.live.db")
def test_process_pending_claims_confirms_successful_claim(mock_db):
    """Successful claim → status=closed, balance credited, tx hash recorded."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.is_condition_redeemable.return_value = True
    mock_claimer.get_token_balance.return_value = 20_000_000  # 20 wcol = $20
    mock_claimer.claim_winnings.return_value = "0xabc123"
    mock_claimer.check_tx_status.return_value = "confirmed"

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "token_id": "12345",
        "claim_status": "claim_pending", "claim_retries": 0,
        "claim_last_attempt": None, "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    update = mock_db.update_trade.call_args[0][1]
    assert update["status"] == "closed"
    assert update["claim_status"] == "claim_confirmed"
    assert update["claim_tx_hash"] == "0xabc123"
    assert "closed_at" in update
    mock_db.update_balance.assert_called_once_with(20.0)  # balance_raw/1e6


@patch("executor.live.db")
def test_process_pending_claims_retries_on_failure(mock_db):
    """Failed claim → increment retries, record last_attempt, stay pending."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0
    mock_claimer.is_condition_redeemable.return_value = True
    mock_claimer.get_token_balance.return_value = 20_000_000
    mock_claimer.claim_winnings.return_value = None  # tx submission failed

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    trade = {
        "id": 10, "market_id": "0x" + "ab" * 32,
        "market_name": "Will NYC be above 60F?",
        "direction": "YES", "shares": 20.0, "size_usdc": 10.0,
        "token_id": "12345",
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
def test_process_pending_claims_fails_after_max_retries(mock_db):
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
        "claim_last_attempt": "2026-04-01T00:00:00",
        "claim_tx_hash": None,
    }
    mock_db.get_pending_claims.return_value = [trade]

    ex.process_pending_claims()

    update = mock_db.update_trade.call_args[0][1]
    assert update["claim_status"] == "claim_failed"


@patch("executor.live.db")
def test_process_pending_claims_skips_if_in_backoff(mock_db):
    """Skip claim if not enough time has elapsed since last attempt."""
    from executor.live import LiveExecutor

    mock_claimer = MagicMock()
    mock_claimer.get_matic_balance.return_value = 1.0

    ex = LiveExecutor.__new__(LiveExecutor)
    ex._client = MagicMock()
    ex._claimer = mock_claimer

    # Last attempt was 1 minute ago, backoff[0] = 5 minutes
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

    mock_claimer.claim_winnings.assert_not_called()
    mock_db.update_trade.assert_not_called()


@patch("executor.live.db")
def test_process_pending_claims_defers_on_low_matic(mock_db):
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


# ── Account value includes claim_pending ────────────────────────────────────


def test_get_open_trades_excludes_claim_pending():
    """claim_pending trades should NOT appear in get_open_trades (no re-resolution)."""
    import db
    db.init_db()

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

    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE market_id = 'test-claim-exclude'")


def test_record_account_value_includes_claim_pending():
    """Account value should include claim_pending positions."""
    import db
    db.init_db()

    # Get baseline: current account value without our test trade
    balance_before = db.get_balance()

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
    tid = db.insert_trade(trade)

    db.record_account_value()

    history = db.get_balance_history()
    latest = history[-1]["amount"]
    # Should include the claim_pending position: 20 shares * $1.00 = +$20
    assert latest >= balance_before + 20.0 - 0.01

    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
