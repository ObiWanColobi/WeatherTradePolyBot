"""Uniform cost model: depth-walk fills + dual fee regime + gas. Conservative bias."""
from __future__ import annotations
from bakeoff.harness.depth import walk_book

OPTIMISTIC_FEE = 0.0
PESSIMISTIC_FEE = 0.05  # verified weather policy rate (Task 0); not yet live but a strategy must survive it
FIXED_COST_USD = 0.004


def cost_fill(levels, shares_wanted, fee_rate, side, fixed_cost=FIXED_COST_USD):
    w = walk_book(levels, shares_wanted)
    shares = w["filled_shares"]
    p = w["avg_price"]
    fee = shares * fee_rate * p * (1.0 - p)
    gas = fixed_cost if shares > 0 else 0.0
    net = shares * p + fee + gas
    return {
        "filled_shares": shares,
        "avg_price": p,
        "fee_usd": fee,
        "gas_usd": gas,
        "unfilled_shares": w["unfilled_shares"],
        "net_cost_usd": net,
    }
