"""Tests for the on-chain claim lifecycle."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


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
