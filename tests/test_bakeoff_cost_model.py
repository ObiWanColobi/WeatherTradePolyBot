import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.cost_model import cost_fill, OPTIMISTIC_FEE, PESSIMISTIC_FEE, TOUCH_CAP_USD

def test_zero_fee_regime_charges_only_touch_and_gas():
    r = cost_fill(0.50, shares_wanted=10.0, fee_rate=OPTIMISTIC_FEE, side="buy")
    assert r["fee_usd"] == pytest.approx(0.0)
    assert r["gas_usd"] == pytest.approx(0.004)
    assert r["filled_shares"] == pytest.approx(10.0)  # 10*0.50 = $5 < $50 cap
    assert r["net_cost_usd"] == pytest.approx(10.0 * 0.50 + 0.004)

def test_pessimistic_fee_uses_touch_p_times_1_minus_p():
    r = cost_fill(0.50, shares_wanted=10.0, fee_rate=PESSIMISTIC_FEE, side="buy")
    # fee = filled * rate * p * (1-p) = 10 * 0.05 * 0.5 * 0.5 = 0.125
    assert r["fee_usd"] == pytest.approx(0.125)

def test_hard_cap_makes_excess_unfillable():
    # want 200 shares @ $0.50 = $100 notional, but cap is $50 -> only 100 shares fillable
    r = cost_fill(0.50, shares_wanted=200.0, fee_rate=OPTIMISTIC_FEE, side="buy", cap_usd=50.0)
    assert r["filled_shares"] == pytest.approx(100.0)   # 50 / 0.50
    assert r["unfilled_shares"] == pytest.approx(100.0)

def test_no_touch_price_is_unfillable():
    r = cost_fill(None, shares_wanted=10.0, fee_rate=OPTIMISTIC_FEE, side="buy")
    assert r["filled_shares"] == pytest.approx(0.0)
    assert r["unfilled_shares"] == pytest.approx(10.0)
