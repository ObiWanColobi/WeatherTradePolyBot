"""Tests for the shared executor factory."""
import pytest
from unittest.mock import patch, MagicMock


def test_create_executor_returns_paper_by_default():
    """Default TRADING_MODE='paper' returns PaperExecutor."""
    with patch("executor.TRADING_MODE", "paper"):
        from executor import create_executor
        from executor.paper import PaperExecutor
        ex = create_executor()
        assert isinstance(ex, PaperExecutor)


def test_create_executor_returns_live_when_configured(monkeypatch):
    """TRADING_MODE='live' returns LiveExecutor (mocked init)."""
    mock_clob = MagicMock()
    mock_clob.return_value.get_balance_allowance.return_value = {
        "allowance": "1000000",
        "balance": "50000000",
    }

    with patch("executor.TRADING_MODE", "live"), \
         patch("executor.live.ClobClient", mock_clob), \
         patch("executor.live.Claimer"), \
         patch("executor.live.WALLET_PRIVATE_KEY", "0xfakekey"):
        from executor import create_executor
        from executor.live import LiveExecutor
        ex = create_executor()
        assert isinstance(ex, LiveExecutor)
