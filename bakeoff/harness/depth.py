"""Depth-walk fills over captured top-3 orderbook levels. Never invents liquidity."""
from __future__ import annotations
import json


def walk_book(levels, shares_wanted: float) -> dict:
    """Walk price levels in order, filling up to shares_wanted.

    levels: list of (price, size) already sorted best-first.
    Returns {filled_shares, avg_price, unfilled_shares}.
    """
    remaining = float(shares_wanted)
    cost = 0.0
    filled = 0.0
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(remaining, float(size))
        cost += take * float(price)
        filled += take
        remaining -= take
    avg = (cost / filled) if filled > 0 else 0.0
    return {
        "filled_shares": filled,
        "avg_price": avg,
        "unfilled_shares": max(0.0, remaining),
    }


def parse_levels(ob_json, fallback_price, side: str):
    """JSON top-3 → [(price,size),...] best-first. side in {'buy','sell'}.

    For buys we consume the ASK side (ascending price); for sells the BID side
    (descending price). If JSON is missing, return a zero-size synthetic level at
    fallback_price so the caller registers it as unfillable depth.
    """
    if ob_json:
        raw = json.loads(ob_json)
        lv = [(float(p), float(s)) for p, s in raw]
        lv.sort(key=lambda x: x[0], reverse=(side == "sell"))
        return lv
    if fallback_price is None:
        return []
    return [(float(fallback_price), 0.0)]
