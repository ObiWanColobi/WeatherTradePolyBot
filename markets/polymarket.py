import json
import requests
from datetime import datetime, timezone
from config import (
    POLYMARKET_GAMMA_API,
    POLYMARKET_CLOB_API,
    POLYMARKET_DATA_API,
    MIN_LIQUIDITY_USDC,
    MAX_HOURS_TO_CLOSE,
    MIN_HOURS_TO_CLOSE,
)

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-bot/1.0"})


def get_active_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    try:
        resp = _session.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"[polymarket] Failed to fetch markets: {e}")
        return []

    now    = datetime.now(timezone.utc)
    result = []

    for m in markets:
        try:
            end_date_str = m.get("endDate") or m.get("end_date_iso")
            if not end_date_str:
                continue

            end_date        = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_remaining = (end_date - now).total_seconds() / 3600

            if not (MIN_HOURS_TO_CLOSE <= hours_remaining <= MAX_HOURS_TO_CLOSE):
                continue

            liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if liquidity < MIN_LIQUIDITY_USDC:
                continue

            # API returns outcomes/prices/tokenIds as JSON-encoded strings
            outcomes   = _parse_json_field(m.get("outcomes", "[]"))
            prices     = _parse_json_field(m.get("outcomePrices", "[]"))
            token_ids  = _parse_json_field(m.get("clobTokenIds", "[]"))

            yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None)
            if yes_idx is None or yes_idx >= len(prices):
                continue

            price    = float(prices[yes_idx])
            token_id = token_ids[yes_idx] if yes_idx < len(token_ids) else None

            if not (0 < price < 1):
                continue

            events = m.get("events") or []
            event_slug = events[0].get("slug") if events else None
            slug = event_slug or m.get("slug") or ""
            result.append({
                "id":              m.get("conditionId") or m.get("id"),
                "question":        m.get("question", ""),
                "token_id":        token_id,
                "price":           price,
                "liquidity":       liquidity,
                "volume":          float(m.get("volumeNum") or m.get("volume") or 0),
                "end_date":        end_date_str,
                "hours_remaining": hours_remaining,
                "category":        m.get("category", ""),
                "market_url":      f"https://polymarket.com/event/{slug}" if slug else "",
            })
        except Exception:
            continue

    return result


def _parse_json_field(value) -> list:
    """Polymarket returns several fields as JSON-encoded strings — parse them safely."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def simulate_fill(token_id: str, size_usdc: float, mid_price: float) -> tuple[float, float, float]:
    """
    Walk the live ask side of the order book to estimate fill quality for a BUY order.

    Returns:
        avg_fill_price : weighted average price across consumed levels
        slippage_pct   : (avg_fill - mid) / mid  — fraction of mid price paid as slippage
        fillable_usdc  : how much of size_usdc the book can actually absorb
                         (may be < size_usdc if the book is thin)
    """
    try:
        book      = get_orderbook(token_id)
        raw_asks  = book.get("asks", [])
        levels    = sorted(
            [{"price": float(l["price"]), "size": float(l["size"])}
             for l in raw_asks if l.get("price") and l.get("size")],
            key=lambda x: x["price"],
        )
    except Exception:
        return mid_price, 0.0, size_usdc

    remaining     = size_usdc
    total_cost    = 0.0
    total_shares  = 0.0

    for lvl in levels:
        if lvl["price"] <= 0:
            continue
        available = lvl["size"] * lvl["price"]
        fill      = min(remaining, available)
        shares    = fill / lvl["price"]
        total_cost   += fill
        total_shares += shares
        remaining    -= fill
        if remaining < 0.01:
            break

    fillable_usdc = size_usdc - remaining

    if total_shares == 0 or mid_price <= 0:
        return mid_price, 0.0, fillable_usdc

    avg_fill     = total_cost / total_shares
    slippage_pct = (avg_fill - mid_price) / mid_price

    return avg_fill, slippage_pct, fillable_usdc


def get_orderbook(token_id: str) -> dict:
    """Fetch the live order book for a YES token. Used by the fill simulator."""
    try:
        resp = _session.get(
            f"{POLYMARKET_CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[polymarket] Failed to fetch orderbook for {token_id}: {e}")
        return {"bids": [], "asks": []}


def get_midpoint(token_id: str) -> float | None:
    for delay in (0, 3, 8):
        try:
            if delay:
                import time; time.sleep(delay)
            resp = _session.get(
                f"{POLYMARKET_CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return float(resp.json().get("mid", 0))
        except Exception:
            continue
    return None


def get_market_trades(condition_id: str, limit: int = 500) -> list[dict]:
    """
    Fetch recent trades for a market via the public Data API (no auth required).
    Takes the condition_id (market_id), not the token_id.

    Returns a list of dicts with keys:
        maker, taker, price, side, size, timestamp
    """
    try:
        resp = _session.get(
            f"{POLYMARKET_DATA_API}/trades",
            params={"market": condition_id, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[polymarket] get_market_trades failed for {condition_id}: {e}")
        return []

    result = []
    for t in raw if isinstance(raw, list) else raw.get("data", []):
        try:
            result.append({
                "wallet":    t.get("proxyWallet", ""),
                "pseudonym": t.get("pseudonym", ""),
                "name":      t.get("name", ""),
                "side":      t.get("side", ""),
                "outcome":   t.get("outcome", ""),
                "price":     float(t.get("price", 0)),
                "size":      float(t.get("size", 0)),
                "token_id":  t.get("asset", ""),
                "timestamp": t.get("timestamp", 0),
            })
        except Exception:
            continue
    return result


def get_wallet_activity(address: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """
    Fetch trade activity for a wallet across all markets via the Data API.

    Returns a list of dicts with keys:
        market_id, token_id, side, price, size, timestamp
    """
    try:
        resp = _session.get(
            f"{POLYMARKET_DATA_API}/activity",
            params={"user": address, "limit": limit, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[polymarket] get_wallet_activity failed for {address}: {e}")
        return []

    result = []
    for t in raw if isinstance(raw, list) else raw.get("data", []):
        try:
            result.append({
                "market_id": t.get("conditionId") or t.get("market") or t.get("market_id", ""),
                "token_id":  t.get("asset") or t.get("token_id", ""),
                "side":      t.get("side", ""),
                "price":     float(t.get("price", 0)),
                "size":      float(t.get("size", 0) or t.get("amount", 0)),
                "timestamp": t.get("timestamp") or t.get("created_at", ""),
            })
        except Exception:
            continue
    return result


def get_wallet_positions(address: str) -> list[dict]:
    """
    Fetch current open positions for a wallet via the Data API.

    Returns a list of dicts with keys:
        market_id, token_id, outcome, size, avg_price, current_price, value_usdc
    """
    try:
        resp = _session.get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": address, "sizeThreshold": "0.01"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[polymarket] get_wallet_positions failed for {address}: {e}")
        return []

    result = []
    for p in raw if isinstance(raw, list) else raw.get("data", []):
        try:
            result.append({
                "market_id":     p.get("conditionId") or p.get("market_id", ""),
                "token_id":      p.get("asset") or p.get("token_id", ""),
                "outcome":       p.get("outcome", ""),
                "size":          float(p.get("size", 0)),
                "avg_price":     float(p.get("avgPrice") or p.get("avg_price", 0)),
                "current_price": float(p.get("currentPrice") or p.get("current_price", 0)),
                "value_usdc":    float(p.get("value") or p.get("value_usdc", 0)),
            })
        except Exception:
            continue
    return result


def get_market_by_id(market_id: str) -> dict | None:
    """Fetch a single market by condition ID. Used for ensemble and resolver checks."""
    try:
        # Use the conditionId directly as a path segment.
        # The ?conditionId= query-param approach does not filter — it silently
        # returns unrelated markets, so we use the REST path instead.
        resp = _session.get(
            f"{POLYMARKET_GAMMA_API}/markets/{market_id}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Path endpoint returns a single object; bulk endpoint returns a list.
        m = data[0] if isinstance(data, list) and data else data
        if not m or not isinstance(m, dict):
            return None

        outcomes  = _parse_json_field(m.get("outcomes", "[]"))
        prices    = _parse_json_field(m.get("outcomePrices", "[]"))
        token_ids = _parse_json_field(m.get("clobTokenIds", "[]"))

        yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None)
        if yes_idx is None or yes_idx >= len(prices):
            return None

        price    = float(prices[yes_idx])
        token_id = token_ids[yes_idx] if yes_idx < len(token_ids) else None

        events = m.get("events") or []
        event_slug = events[0].get("slug") if events else None
        slug = event_slug or m.get("slug") or ""
        return {
            "id":         m.get("conditionId") or m.get("id"),
            "question":   m.get("question", ""),
            "token_id":   token_id,
            "price":      price,
            "liquidity":  float(m.get("liquidityNum") or m.get("liquidity") or 0),
            "volume":     float(m.get("volume24hr") or m.get("volumeNum") or 0),
            "end_date":   m.get("endDateIso") or m.get("endDate"),
            "resolved":   m.get("closed", False),
            "market_url": f"https://polymarket.com/event/{slug}" if slug else "",
        }
    except Exception:
        return None


def get_market_tokens(condition_id: str) -> dict | None:
    """
    Fetch YES/NO token IDs for a market via CLOB.
    Returns {"yes_token_id": "...", "no_token_id": "..."} or None on failure.
    """
    try:
        resp = requests.get(
            f"{POLYMARKET_CLOB_API}/markets/{condition_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        tokens = data.get("tokens", [])
        result = {}
        for t in tokens:
            outcome = (t.get("outcome") or "").upper()
            if outcome == "YES":
                result["yes_token_id"] = t.get("token_id")
            elif outcome == "NO":
                result["no_token_id"] = t.get("token_id")
        return result if result.get("yes_token_id") else None
    except Exception:
        return None
