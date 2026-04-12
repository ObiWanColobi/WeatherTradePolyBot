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
