from shotgun.sizing import apply_liquidity_cap


def test_liquidity_cap_trims_oversized_bets():
    bets = [
        {"stake_usd": 10.0, "liquidity_num": 1000.0},  # cap = 0.10*1000=100, no trim
        {"stake_usd": 10.0, "liquidity_num": 44.0},    # cap = 0.10*44=4.4, trim to 4.4
    ]
    out = apply_liquidity_cap(bets, cap_frac=0.10)
    assert out[0]["stake_usd"] == 10.0
    assert abs(out[1]["stake_usd"] - 4.4) < 1e-6


def test_liquidity_cap_none_liquidity_left_unchanged():
    bets = [{"stake_usd": 10.0, "liquidity_num": None}]
    out = apply_liquidity_cap(bets, cap_frac=0.10)
    assert out[0]["stake_usd"] == 10.0


def test_liquidity_cap_zero_or_negative_liquidity_left_unchanged():
    bets = [{"stake_usd": 10.0, "liquidity_num": 0.0}]
    out = apply_liquidity_cap(bets, cap_frac=0.10)
    assert out[0]["stake_usd"] == 10.0


def test_liquidity_cap_disabled_when_frac_zero():
    bets = [{"stake_usd": 10.0, "liquidity_num": 44.0}]
    out = apply_liquidity_cap(bets, cap_frac=0.0)
    assert out[0]["stake_usd"] == 10.0


def test_liquidity_cap_does_not_raise_stake_below_cap():
    # a small stake under the cap is left alone (cap only trims down, never up)
    bets = [{"stake_usd": 1.0, "liquidity_num": 1000.0}]
    out = apply_liquidity_cap(bets, cap_frac=0.10)
    assert out[0]["stake_usd"] == 1.0
