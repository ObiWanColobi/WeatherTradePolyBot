import time
from unittest.mock import patch, MagicMock


def test_clob_rate_limit_enforces_delay():
    """Two rapid CLOB calls should be spaced by at least clob_inter_request_delay."""
    with patch("executor.live.ClobClient") as mock_clob_cls, \
         patch("executor.live.WALLET_PRIVATE_KEY", "0x" + "ab" * 32), \
         patch("executor.live.POLYMARKET_CLOB_API", "https://fake"), \
         patch("executor.live.CHAIN_ID", 137), \
         patch("executor.live.Claimer"), \
         patch("executor.live.WEATHER", {
             "clob_inter_request_delay": 0.2,
             "live_kelly_max_bet_usdc": 25,
             "live_risk_daily_loss_limit_pct": 0.05,
         }), \
         patch("executor.live.polymarket"):
        mock_client = MagicMock()
        mock_clob_cls.return_value = mock_client
        mock_client.get_balance_allowance.return_value = {"balance": "100000000", "allowance": "100000000"}
        mock_client.create_order.return_value = {"signed": True}
        mock_client.post_order.return_value = {"orderID": "abc123", "status": "matched"}
        mock_client.get_order.return_value = {
            "status": "matched",
            "associate_trades": [{"price": "0.60", "size": "10", "fee": "0.01"}],
        }

        from executor.live import LiveExecutor
        ex = LiveExecutor()

        # First call — should be immediate
        t0 = time.time()
        ex._throttle_clob()
        t1 = time.time()
        assert (t1 - t0) < 0.1  # no delay on first call

        # Second call — should be delayed
        ex._throttle_clob()
        t2 = time.time()
        assert (t2 - t1) >= 0.15  # at least ~200ms delay
