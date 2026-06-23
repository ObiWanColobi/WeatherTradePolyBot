"""Uniform cost model: single-touch fills + hard notional cap + dual fee regime + gas.

No orderbook depth was ever captured (columns are NULL), so we do NOT walk a book.
A fill happens at the touch price (best_ask for buys, best_bid for sells) up to a
hard notional cap (TOUCH_CAP_USD); any size beyond the cap is UNFILLABLE. This is the
conservative no-depth honesty rule: we never invent liquidity we did not observe.
"""
from __future__ import annotations

OPTIMISTIC_FEE = 0.0
PESSIMISTIC_FEE = 0.05  # verified weather policy rate (Task 0); not yet live but a strategy must survive it
FIXED_COST_USD = 0.004
TOUCH_CAP_USD = 50.0  # max notional assumed fillable at the touch (no depth data to walk)


def cost_fill(touch_price, shares_wanted, fee_rate, side, *,
              cap_usd=TOUCH_CAP_USD, fixed_cost=FIXED_COST_USD):
    """Fill at a single touch price up to a hard notional cap; rest is unfillable.

    touch_price: best_ask for a buy, best_bid for a sell (the price actually paid).
    shares_wanted: intended share count.
    Returns {filled_shares, avg_price, fee_usd, gas_usd, unfilled_shares, net_cost_usd}.
    """
    if touch_price is None or touch_price <= 0:
        return {"filled_shares": 0.0, "avg_price": 0.0, "fee_usd": 0.0,
                "gas_usd": 0.0, "unfilled_shares": float(shares_wanted), "net_cost_usd": 0.0}
    p = float(touch_price)
    max_shares = cap_usd / p
    filled = min(float(shares_wanted), max_shares)
    unfilled = max(0.0, float(shares_wanted) - filled)
    fee = filled * fee_rate * p * (1.0 - p)
    gas = fixed_cost if filled > 0 else 0.0
    net = filled * p + fee + gas
    return {
        "filled_shares": filled,
        "avg_price": p,
        "fee_usd": fee,
        "gas_usd": gas,
        "unfilled_shares": unfilled,
        "net_cost_usd": net,
    }
