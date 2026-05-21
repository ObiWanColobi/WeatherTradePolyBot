"""Unit tests for snapshot-backtest P&L math. Pure functions, no DB."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "research_db"))

import pytest

from snapshot_pnl import simulate_fill, resolve_pnl


def test_simulate_fill_applies_slippage():
    f = simulate_fill(mid_price=0.30, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    assert f["fill_price"] == pytest.approx(0.32)
    # fee = 1.0 * 0.02 = 0.02; net stake invested = 0.98; shares = 0.98 / 0.32
    assert f["shares"] == pytest.approx(0.98 / 0.32)
    assert f["fee_usd"] == pytest.approx(0.02)


def test_simulate_fill_zero_slippage_zero_fee():
    f = simulate_fill(mid_price=0.50, stake_usd=1.0, slippage_cents=0.0, fee_pct=0.0)
    assert f["fill_price"] == pytest.approx(0.50)
    assert f["shares"] == pytest.approx(2.0)
    assert f["fee_usd"] == pytest.approx(0.0)


def test_simulate_fill_degenerate_zero_fill_price():
    # mid=0, slip=0 → fill_price=0; shares clamp to 0 (avoid div-by-zero)
    f = simulate_fill(mid_price=0.0, stake_usd=1.0, slippage_cents=0.0, fee_pct=0.02)
    assert f["fill_price"] == 0.0
    assert f["shares"] == 0.0


def test_resolve_pnl_winning_trade():
    # Buy at 0.32 effective, win → shares pay $1 each → P&L = shares*1 - stake
    f = simulate_fill(mid_price=0.30, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    pnl = resolve_pnl(f, won=True)
    expected_shares = 0.98 / 0.32
    assert pnl == pytest.approx(expected_shares * 1.0 - 1.0)


def test_resolve_pnl_losing_trade():
    # Buy at 0.32, lose → shares pay $0 → loss = full stake (fee included)
    f = simulate_fill(mid_price=0.30, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    pnl = resolve_pnl(f, won=False)
    assert pnl == pytest.approx(-1.0)


def test_resolve_pnl_breakeven_market_50_50():
    # If we pay 0.50 effective for a true 50/50, average P&L over both outcomes
    # should equal -fee (the only friction). Math:
    #   shares = 0.98 / 0.50 = 1.96
    #   win:  1.96 * 1 - 1.0 = +0.96
    #   loss: -1.0
    #   avg:  (0.96 + -1.0) / 2 = -0.02 == -fee
    f = simulate_fill(mid_price=0.48, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    pnl_win = resolve_pnl(f, won=True)
    pnl_loss = resolve_pnl(f, won=False)
    assert (pnl_win + pnl_loss) / 2 == pytest.approx(-0.02, abs=1e-6)


def test_resolve_pnl_high_edge_positive_ev():
    # Buy at 0.20 effective when true prob is 0.40 → EV strongly positive
    #   shares = 0.98 / 0.20 = 4.9
    #   win pnl = 4.9 - 1.0 = 3.9; loss pnl = -1.0
    #   ev at 40% WR = 0.4*3.9 + 0.6*-1.0 = 1.56 - 0.60 = +0.96
    f = simulate_fill(mid_price=0.18, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    pnl_win = resolve_pnl(f, won=True)
    pnl_loss = resolve_pnl(f, won=False)
    ev = 0.40 * pnl_win + 0.60 * pnl_loss
    assert ev == pytest.approx(0.96, abs=0.01)


def test_simulate_fill_scales_with_stake():
    # P&L should scale linearly with stake size at fixed price+slip+fee
    f1 = simulate_fill(mid_price=0.30, stake_usd=1.0, slippage_cents=0.02, fee_pct=0.02)
    f2 = simulate_fill(mid_price=0.30, stake_usd=10.0, slippage_cents=0.02, fee_pct=0.02)
    assert f2["shares"] == pytest.approx(f1["shares"] * 10)
    assert resolve_pnl(f2, won=True) == pytest.approx(resolve_pnl(f1, won=True) * 10)
    assert resolve_pnl(f2, won=False) == pytest.approx(resolve_pnl(f1, won=False) * 10)
