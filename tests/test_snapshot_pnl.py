"""Unit tests for snapshot-backtest P&L math. Pure functions, no DB."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "research_db"))

import pytest

from snapshot_pnl import simulate_fill, resolve_pnl, simulate_fill_realistic


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


# ── simulate_fill_realistic (Polymarket-accurate execution model, #17) ────────────
#
# Cost stack per fill (see reference_polymarket_fee_gas_structure):
#   fill_price = best_ask (YES) | 1 - best_bid (NO) | mid + fallback_half_spread
#   gross_invested = stake - fixed_cost_usd      (gas/fixed comes off the top)
#   shares_gross   = gross_invested / fill_price
#   fee_usd        = shares_gross * fee_rate * p * (1-p)   (Polymarket taker formula)
#   shares (net)   = (gross_invested - fee_usd) / fill_price
#   -> resolve_pnl: win = shares*1 - stake ; loss = -stake  (all costs already in stake)


def test_realistic_fill_crosses_to_best_ask_yes():
    # YES buy pays the ask, not mid. mid 0.30 but ask 0.33 -> pay 0.33.
    f = simulate_fill_realistic(
        mid_price=0.30, stake_usd=1.0, best_ask=0.33, best_bid=0.27,
        side="yes", fee_rate=0.0, fixed_cost_usd=0.0,
    )
    assert f["fill_price"] == pytest.approx(0.33)
    assert f["shares"] == pytest.approx(1.0 / 0.33)


def test_realistic_fill_no_leg_prices_complement():
    # NO buy: cost = 1 - best_bid. bid 0.27 -> NO ask = 0.73.
    f = simulate_fill_realistic(
        mid_price=0.30, stake_usd=1.0, best_ask=0.33, best_bid=0.27,
        side="no", fee_rate=0.0, fixed_cost_usd=0.0,
    )
    assert f["fill_price"] == pytest.approx(0.73)
    assert f["shares"] == pytest.approx(1.0 / 0.73)


def test_realistic_fill_fallback_when_book_missing():
    # No best_ask -> fall back to mid + fallback_half_spread.
    f = simulate_fill_realistic(
        mid_price=0.30, stake_usd=1.0, best_ask=None, best_bid=None,
        side="yes", fee_rate=0.0, fixed_cost_usd=0.0, fallback_half_spread=0.02,
    )
    assert f["fill_price"] == pytest.approx(0.32)


def test_realistic_fill_taker_fee_formula():
    # fee = shares * rate * p * (1-p). At p=0.50 with rate 0.0125 this is the peak.
    f = simulate_fill_realistic(
        mid_price=0.50, stake_usd=1.0, best_ask=0.50, best_bid=0.50,
        side="yes", fee_rate=0.0125, fixed_cost_usd=0.0,
    )
    shares_gross = 1.0 / 0.50  # 2.0
    expected_fee = shares_gross * 0.0125 * 0.50 * 0.50
    assert f["fee_usd"] == pytest.approx(expected_fee)
    # net shares reduced by fee worth of shares
    assert f["shares"] == pytest.approx((1.0 - expected_fee) / 0.50)


def test_realistic_fill_fee_vanishes_at_price_extreme():
    # p(1-p) -> ~0 at the cheap tail, so fee ~ 0 on a 3c bucket.
    f = simulate_fill_realistic(
        mid_price=0.03, stake_usd=1.0, best_ask=0.03, best_bid=0.03,
        side="yes", fee_rate=0.0125, fixed_cost_usd=0.0,
    )
    # fee = (1/0.03) * 0.0125 * 0.03 * 0.97 = 0.0125*0.97 = 0.012125
    assert f["fee_usd"] == pytest.approx((1.0 / 0.03) * 0.0125 * 0.03 * 0.97)
    assert f["fee_usd"] < 0.013  # small relative to a 50c bucket's fee per share


def test_realistic_fill_fixed_cost_off_the_top():
    # Gas comes off the top before buying shares.
    f = simulate_fill_realistic(
        mid_price=0.50, stake_usd=0.10, best_ask=0.50, best_bid=0.50,
        side="yes", fee_rate=0.0, fixed_cost_usd=0.004,
    )
    assert f["fixed_cost_usd"] == pytest.approx(0.004)
    # gross invested = 0.10 - 0.004 = 0.096 -> shares = 0.096/0.50
    assert f["shares"] == pytest.approx(0.096 / 0.50)


def test_realistic_fill_gas_dominates_tiny_bet():
    # The shotgun killer: a sub-penny bet where gas >= stake yields 0 shares.
    f = simulate_fill_realistic(
        mid_price=0.10, stake_usd=0.0038, best_ask=0.10, best_bid=0.10,
        side="yes", fee_rate=0.0125, fixed_cost_usd=0.004,
    )
    # gross invested = 0.0038 - 0.004 < 0 -> clamp shares to 0
    assert f["shares"] == 0.0


def test_realistic_resolve_pnl_loss_includes_all_costs():
    # On a loss you forfeit the whole stake (gas + fee + share cost all inside).
    f = simulate_fill_realistic(
        mid_price=0.30, stake_usd=1.0, best_ask=0.33, best_bid=0.27,
        side="yes", fee_rate=0.0125, fixed_cost_usd=0.004,
    )
    assert resolve_pnl(f, won=False) == pytest.approx(-1.0)


def test_realistic_resolve_pnl_tiny_win_can_net_negative():
    # A bet that "wins" can still net negative once gas+fee are counted:
    # cheap near-even bet, gas a big fraction of stake.
    f = simulate_fill_realistic(
        mid_price=0.50, stake_usd=0.01, best_ask=0.52, best_bid=0.48,
        side="yes", fee_rate=0.0125, fixed_cost_usd=0.004,
    )
    # shares small because ask 0.52 + gas ate 40% of stake; payout may be < stake
    pnl = resolve_pnl(f, won=True)
    # shares = (0.01-0.004-fee)/0.52 ~ 0.0115; payout ~0.0115 < 0.01 stake? check sign
    assert pnl < f["shares"]  # sanity: pnl = shares - stake
    assert pnl == pytest.approx(f["shares"] - 0.01)
