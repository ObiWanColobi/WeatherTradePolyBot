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
