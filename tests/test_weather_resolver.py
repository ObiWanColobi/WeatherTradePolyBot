"""Tests for weather_resolver.run_resolve_pass — focuses on the exit_pending
rescue path added in 2026-05-11."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


def _past_close_iso(hours_ago: int = 24) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _stuck_exit_pending():
    return {
        "id": 51, "market_id": "cond-tokyo",
        "market_name": "Tokyo NO 25C May 10",
        "token_id": "tok-no-tokyo", "direction": "NO",
        "shares": 85.2, "size_usdc": 75.83,
        "fill_price": 0.89, "end_date": _past_close_iso(24),
        "exit_order_id": "0xc0b83e26091bb03a",
        "exit_order_price": 0.01,
        "exit_order_placed_at": _past_close_iso(12),
        "status": "exit_pending",
        "opened_at": _past_close_iso(60),
        "city": "tokyo", "threshold": ">=25C",
    }


@patch("weather_resolver.get_midpoint")
@patch("weather_resolver.db")
def test_run_resolve_pass_settles_stuck_exit_pending(mock_db, mock_mid):
    """An exit_pending row past end_date with a resolved market must be settled."""
    import weather_resolver

    mock_db.get_open_trades.return_value = []
    mock_db.get_exit_pending_trades.return_value = [_stuck_exit_pending()]
    # NO token midpoint is ~0.001 → market resolved YES (NO lost)
    mock_mid.return_value = 0.001

    executor = MagicMock()
    settled = weather_resolver.run_resolve_pass(executor)

    assert settled == 1
    # exit_order_* cleared and status reverted before settlement
    cleanup_calls = [
        c for c in mock_db.update_trade.call_args_list
        if c[0][1].get("exit_order_id") is None
    ]
    assert len(cleanup_calls) == 1, "exit_order_id should be cleared once"
    assert cleanup_calls[0][0][0] == 51

    # settle_resolved called with resolved_yes=True (Tokyo did hit 25C → NO lost)
    executor.settle_resolved.assert_called_once()
    args, _ = executor.settle_resolved.call_args
    assert args[0]["id"] == 51
    assert args[1] is True


@patch("weather_resolver.get_midpoint")
@patch("weather_resolver.db")
def test_run_resolve_pass_skips_exit_pending_not_past_close(mock_db, mock_mid):
    """An exit_pending row that is NOT past end_date should be left alone."""
    import weather_resolver

    future_trade = _stuck_exit_pending()
    future_trade["end_date"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    mock_db.get_open_trades.return_value = []
    mock_db.get_exit_pending_trades.return_value = [future_trade]
    mock_mid.return_value = 0.001

    executor = MagicMock()
    settled = weather_resolver.run_resolve_pass(executor)

    assert settled == 0
    executor.settle_resolved.assert_not_called()
    mock_db.update_trade.assert_not_called()
