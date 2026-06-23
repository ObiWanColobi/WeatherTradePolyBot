import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.cost_model import cost_fill, OPTIMISTIC_FEE, PESSIMISTIC_FEE

def test_zero_fee_regime_charges_only_book_and_gas():
    r = cost_fill([(0.50, 100.0)], shares_wanted=10.0, fee_rate=OPTIMISTIC_FEE, side="buy")
    assert r["fee_usd"] == pytest.approx(0.0)
    assert r["gas_usd"] == pytest.approx(0.004)
    assert r["net_cost_usd"] == pytest.approx(10.0 * 0.50 + 0.004)

def test_pessimistic_fee_uses_avg_price_p_times_1_minus_p():
    r = cost_fill([(0.50, 100.0)], shares_wanted=10.0, fee_rate=PESSIMISTIC_FEE, side="buy")
    # fee = shares * rate * p * (1-p) = 10 * 0.05 * 0.5 * 0.5 = 0.125
    assert r["fee_usd"] == pytest.approx(0.125)

def test_unfilled_size_propagates():
    r = cost_fill([(0.50, 3.0)], shares_wanted=10.0, fee_rate=OPTIMISTIC_FEE, side="buy")
    assert r["filled_shares"] == pytest.approx(3.0)
    assert r["unfilled_shares"] == pytest.approx(7.0)
