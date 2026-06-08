"""Discover live city-day bucket markets and shape them for plan_fire().
Reuses snapshot_parse for bound/title parsing and polymarket.iter_weather_events
for the Gamma /events/slug discovery walk."""
from __future__ import annotations

import json

from snapshot_parse import parse_bucket_bounds, classify_bucket_type, parse_event_slug
import markets.polymarket as polymarket


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def buckets_from_event(event: dict, city: str, resolution_date: str) -> list[dict]:
    """Shape one event's sub-markets into plan_fire bucket dicts."""
    out = []
    for m in event.get("markets") or []:
        gtitle = m.get("groupItemTitle", "")
        lo_f, hi_f = parse_bucket_bounds(gtitle)
        btype = classify_bucket_type(gtitle)
        is_open_tail = 1 if ((lo_f is None or hi_f is None) and btype == "tail") else 0
        bb, ba = _to_float(m.get("bestBid")), _to_float(m.get("bestAsk"))
        # _to_float returns None when missing; never use truthiness on the
        # numbers themselves (0.0 is falsy — a 0.0 bid would short-circuit `and`
        # and silently drop edge-rich cheap-tail buckets). Use explicit None
        # checks. Both sides valid -> true midpoint; else fall back to best_ask
        # as the price proxy so no/zero-bid tail buckets stay in the spread
        # (the paper fill sim crosses the ask at order time anyway).
        if bb is not None and ba is not None and bb > 0 and ba > 0:
            mid = (bb + ba) / 2
        elif ba is not None and ba > 0:
            mid = ba
        else:
            mid = None
        token_ids = m.get("clobTokenIds")
        yes_tok = no_tok = None
        if token_ids:
            try:
                toks = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
                if len(toks) >= 2:
                    yes_tok, no_tok = toks[0], toks[1]
            except Exception:
                pass
        out.append(dict(
            sub_market_condition_id=m.get("conditionId", ""),
            market_id=str(m.get("id", "")),
            group_item_title=gtitle, bound_lo_f=lo_f, bound_hi_f=hi_f,
            is_open_tail=is_open_tail, mid_price=mid, best_bid=bb, best_ask=ba,
            volume_24h=_to_float(m.get("volume24hr")),
            liquidity_num=_to_float(m.get("liquidityNum")),
            token_id=yes_tok, no_token_id=no_tok,
            city=city, resolution_date=resolution_date,
        ))
    return out


def discover_city_days(cities: list[str], days_ahead: int = 2) -> dict:
    """Return {(city, resolution_date): [bucket dicts]} for live highest-temp events."""
    groups = {}
    for event in polymarket.iter_weather_events(cities, days_ahead=days_ahead, kinds=("highest",)):
        parsed = parse_event_slug(event.get("slug", ""))
        if not parsed:
            continue
        city, rd = parsed["city"], parsed["resolution_date"]
        groups[(city, rd)] = buckets_from_event(event, city, rd)
    return groups
