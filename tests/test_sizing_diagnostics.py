"""Unit tests for kelly_size_with_diagnostics and sizing_decision persistence."""
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from weather_sizing import kelly_size, kelly_size_with_diagnostics


# ── Pure-function diagnostics ────────────────────────────────────────────────

def test_no_edge_returns_zero_with_no_edge_constraint():
    size, diag = kelly_size_with_diagnostics(
        balance=1000.0, model_prob=0.5, market_price=0.6, direction="yes",
        ensemble_n=69, days_to_resolution=0,
    )
    assert size == 0.0
    assert diag["binding_constraint"] == "no_edge"
    assert diag["kelly_raw"] == 0.0


def test_kelly_wrapper_matches_diagnostics_pair():
    """The legacy kelly_size() must return exactly what the diagnostics path produces."""
    args = dict(
        balance=4035.0, model_prob=0.0, market_price=0.12, direction="no",
        ensemble_n=69, days_to_resolution=1, unanimous=True,
    )
    legacy = kelly_size(**args)
    new, _ = kelly_size_with_diagnostics(**args)
    assert legacy == new


def test_unanimous_cap_binds_for_high_conviction_no_at_extreme_price():
    """Toronto-style trade: P(NO)=1.0 (clamped 0.99), price=$0.88 NO, 1 day out, $4035 balance."""
    size, diag = kelly_size_with_diagnostics(
        balance=4035.0, model_prob=0.0, market_price=0.12, direction="no",
        ensemble_n=69, days_to_resolution=1, unanimous=True,
    )
    # Raw kelly is enormous (~80%) so a cap MUST bind.
    assert diag["kelly_raw"] > 0.5
    assert diag["binding_constraint"] in ("cap_per_bet", "cap_balance_pct")
    # And the resulting size matches whichever cap was binding.
    if diag["binding_constraint"] == "cap_per_bet":
        assert size == round(diag["cap_per_bet"], 2)
    else:
        assert size == round(diag["cap_balance_pct"], 2)


def test_min_bet_floor_zeros_out_tiny_kelly():
    """Tiny edge → Kelly produces sub-$5 bet → floor zeroes it out, but diag still tells the story."""
    size, diag = kelly_size_with_diagnostics(
        balance=100.0, model_prob=0.51, market_price=0.50, direction="yes",
        ensemble_n=69, days_to_resolution=3,
    )
    assert size == 0.0
    assert diag["binding_constraint"] == "min_bet_floor"
    assert diag["size_after_caps"] < 5.0
    assert diag["kelly_raw"] > 0


def test_ensemble_margin_dampens_size_proportionally():
    """margin_mult = abs(margin)/5, capped at 1.0."""
    base_args = dict(
        balance=10000.0, model_prob=0.80, market_price=0.50, direction="yes",
        ensemble_n=69, days_to_resolution=0, unanimous=False,
    )
    _, large = kelly_size_with_diagnostics(**base_args, ensemble_margin_c=10.0)
    _, small = kelly_size_with_diagnostics(**base_args, ensemble_margin_c=1.0)
    assert large["margin_mult"] == 1.0
    assert small["margin_mult"] == pytest.approx(0.2)
    # And final size scales accordingly (before any cap binding)
    assert small["size_pre_cap"] == pytest.approx(large["size_pre_cap"] * 0.2, rel=1e-6)


# ── DB persistence ───────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch):
    """Spin up a temp SQLite file, point db.DB_PATH at it, run init_db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        import db as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", path)
        db_mod.init_db()
        yield db_mod
    finally:
        os.unlink(path)


def test_write_and_link_sizing_decision_roundtrip(temp_db):
    sizing_id = temp_db.write_sizing_decision({
        "recorded_at":         "2026-05-04T18:21:00+00:00",
        "market_id":           "0xabc",
        "market_name":         "Will Toronto be 23C?",
        "city":                "toronto",
        "direction":           "no",
        "balance":             4035.32,
        "model_prob":          0.0,
        "market_price":        0.12,
        "p_used":              0.99,
        "price_used":          0.88,
        "ensemble_n":          69,
        "days_to_resolution":  1,
        "ensemble_margin_c":   None,
        "is_unanimous":        1,
        "edge":                0.11,
        "odds":                0.13636,
        "kelly_raw":           0.8067,
        "ensemble_scale":      1.0,
        "horizon_mult":        0.85,
        "margin_mult":         1.0,
        "kelly_fraction":      0.5,
        "kelly_final":         0.3428,
        "size_pre_cap":        1383.0,
        "cap_per_bet":         75.0,
        "cap_balance_pct":     80.71,
        "size_after_caps":     75.0,
        "slippage_reduced_to": 13.21,
        "final_size":          13.21,
        "binding_constraint":  "slippage",
    })
    assert isinstance(sizing_id, int) and sizing_id > 0

    temp_db.link_sizing_decision_to_trade(sizing_id, trade_id=42)

    with temp_db.get_conn() as conn:
        row = conn.execute(
            "SELECT trade_id, binding_constraint, final_size FROM sizing_decisions WHERE id=?",
            (sizing_id,),
        ).fetchone()
    assert row["trade_id"]           == 42
    assert row["binding_constraint"] == "slippage"
    assert row["final_size"]         == pytest.approx(13.21)
