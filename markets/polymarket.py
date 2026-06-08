import json
import time as _time
from typing import Iterator
import requests
from datetime import datetime, timezone, timedelta
from config import (
    POLYMARKET_GAMMA_API,
    POLYMARKET_CLOB_API,
    POLYMARKET_DATA_API,
    MIN_LIQUIDITY_USDC,
    MAX_HOURS_TO_CLOSE,
    MIN_HOURS_TO_CLOSE,
    WEATHER,
)

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-bot/1.0"})

_MONTHS = ['january','february','march','april','may','june','july',
          'august','september','october','november','december']

# Per-event cache so the 60-second poll loop doesn't redo 144 HTTP requests
# every cycle. Events themselves rarely change once created; the bot polls
# token midpoint / orderbook directly for live price data.
_EVENT_CACHE: dict[str, tuple[float, dict | None]] = {}
_EVENT_CACHE_TTL_SEC = 15 * 60   # 15 min


def _city_to_slug(city: str) -> str:
    """Normalise a city name to its Polymarket slug. Reads overrides from
    WEATHER['city_slugs']; defaults to lower+hyphenated."""
    c = (city or "").lower().strip()
    overrides = WEATHER.get("city_slugs") or {}
    return overrides.get(c, c.replace(" ", "-"))


def get_active_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    # DEPRECATED for weather discovery (2026-05-18) — see paginate_active_markets
    # docstring for the index-freeze details. Currently unused in the bot;
    # retained for general-market lookups.
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


def fetch_weather_event(city_slug: str, date, kind: str = "highest") -> dict | None:
    """Fetch a single Polymarket daily-temperature event by deterministic slug.

    Returns the full event dict (with nested `markets`) or None on miss /
    HTTP error. Cached in `_EVENT_CACHE` with 15-min TTL.

    Slug pattern observed 2026-05-18 (after Gamma index froze for new weather
    events): `{kind}-temperature-in-{city_slug}-on-{month}-{day}-{year}`,
    where kind ∈ {highest, lowest} and month is lowercased English name.
    """
    event_slug = (
        f"{kind}-temperature-in-{city_slug}-on-"
        f"{_MONTHS[date.month - 1]}-{date.day}-{date.year}"
    )
    now_ts = _time.time()
    cached = _EVENT_CACHE.get(event_slug)
    if cached and (now_ts - cached[0]) < _EVENT_CACHE_TTL_SEC:
        return cached[1]

    try:
        resp = _session.get(
            f"{POLYMARKET_GAMMA_API}/events/slug/{event_slug}",
            timeout=10,
        )
        if resp.status_code != 200:
            _EVENT_CACHE[event_slug] = (now_ts, None)
            return None
        body = resp.json()
    except Exception as e:
        print(f"[polymarket] fetch_weather_event({event_slug}) failed: {e}")
        _EVENT_CACHE[event_slug] = (now_ts, None)
        return None

    if not body or not isinstance(body, dict) or not body.get("markets"):
        _EVENT_CACHE[event_slug] = (now_ts, None)
        return None

    _EVENT_CACHE[event_slug] = (now_ts, body)
    return body


def iter_weather_events(
    cities: list[str],
    *,
    days_ahead: int = 2,
    kinds: tuple[str, ...] = ("highest", "lowest"),
    request_delay_sec: float = 0.03,
) -> Iterator[dict]:
    """Yield whole event dicts for the (cities × forward-days × kinds) grid via
    `/events/slug/{slug}` lookups.

    Each yielded event is the raw Gamma event dict (has 'slug', 'endDate',
    'markets'). Per-event responses are cached for 15 min (see
    _EVENT_CACHE_TTL_SEC). Cold-pass cost:
    ~len(cities) * (days_ahead + 1) * len(kinds) requests.

    Past-close events are filtered out: Polymarket leaves them
    `acceptingOrders=True` through UMA resolution but the bot can't enter
    them (entry_min_hours_to_close gate) and many have dead CLOB books.
    """
    today = datetime.now(timezone.utc).date()
    now_iso = datetime.now(timezone.utc).isoformat()
    for city in cities:
        cslug = _city_to_slug(city)
        for delta in range(days_ahead + 1):
            d = today + timedelta(days=delta)
            for kind in kinds:
                event = fetch_weather_event(cslug, d, kind=kind)
                if not event:
                    if request_delay_sec > 0:
                        _time.sleep(request_delay_sec)
                    continue
                event_end = event.get("endDate") or ""
                if event_end and event_end < now_iso:
                    if request_delay_sec > 0:
                        _time.sleep(request_delay_sec)
                    continue
                yield event
                if request_delay_sec > 0:
                    _time.sleep(request_delay_sec)


def iter_weather_markets(
    cities: list[str],
    *,
    days_ahead: int = 2,
    kinds: tuple[str, ...] = ("highest", "lowest"),
    request_delay_sec: float = 0.03,
) -> Iterator[dict]:
    """Yield raw Polymarket market dicts for the (cities × forward-days × kinds)
    grid, discovered via direct `/events/slug/{slug}` lookups.

    Delegates the discovery walk to `iter_weather_events`; behavior is preserved
    for existing callers. Each yielded dict is a market from `event["markets"]`
    with two extra keys injected for downstream convenience:
        _event_slug : str — the parent event slug
        _event_end  : str — the event endDate (ISO)

    Per-event responses are cached for 15 min (see _EVENT_CACHE_TTL_SEC).
    Cold-pass cost: ~len(cities) * (days_ahead + 1) * len(kinds) requests.

    Past-close events are filtered out: Polymarket leaves them
    `acceptingOrders=True` through UMA resolution but the bot can't enter
    them (entry_min_hours_to_close gate) and many have dead CLOB books.
    """
    for event in iter_weather_events(
        cities,
        days_ahead=days_ahead,
        kinds=kinds,
        request_delay_sec=request_delay_sec,
    ):
        event_slug = event.get("slug") or ""
        event_end  = event.get("endDate") or ""
        for m in event.get("markets") or []:
            if isinstance(m, dict):
                m["_event_slug"] = event_slug
                m["_event_end"]  = event_end
                yield m


def paginate_active_markets(
    *,
    max_pages: int = 25,
    page_size: int = 100,
    order: str = "volume24hr",
    ascending: bool = False,
    min_volume_24h: float | None = None,
    request_delay_sec: float = 0.0,
) -> Iterator[list[dict]]:
    """
    DEPRECATED for weather-market discovery (2026-05-18).
    Polymarket's Gamma `/markets` index stopped admitting new daily-weather
    events around 2026-05-13 — they are created with
    `active=False, archived=True, accepting_orders=True` and excluded from
    every `/markets?…` query regardless of filter combination. Use
    `iter_weather_markets()` for any weather discovery.

    Still valid for non-weather Gamma walks (e.g. trader_discovery's wallet
    harvest across general markets).

    Yield batches of active, unclosed markets from Gamma /markets.

    Polymarket caps `limit` at 100 per request (observed 2026-05-15). This
    helper advances `offset` by the actual size of each returned batch and
    terminates when the API returns an empty batch OR fewer markets than
    `page_size` — the real end-of-data signal. That keeps the walk correct
    if Polymarket changes the cap again.

    Yields each batch incrementally so callers can filter without holding
    the whole walk in memory.

    Args:
        max_pages:        hard ceiling on number of HTTP requests.
        page_size:        requested limit per request (API may return fewer).
        order:            sort field forwarded to Gamma.
        ascending:        sort direction.
        min_volume_24h:   if set, stop once the last item on a page falls
                          below this floor (only sensible for volume-sorted
                          descending walks).
        request_delay_sec: optional sleep between requests for politeness.
    """
    import time as _time
    offset = 0
    for page in range(max_pages):
        try:
            resp = _session.get(
                f"{POLYMARKET_GAMMA_API}/markets",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "limit":     page_size,
                    "offset":    offset,
                    "order":     order,
                    "ascending": "true" if ascending else "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[polymarket] paginate page {page} (offset={offset}) failed: {e}")
            return

        if not batch:
            return

        yield batch

        got = len(batch)
        offset += got

        if got < page_size:
            return

        if min_volume_24h is not None:
            try:
                if float(batch[-1].get("volume24hr") or 0) < min_volume_24h:
                    return
            except (TypeError, ValueError):
                pass

        if request_delay_sec > 0:
            _time.sleep(request_delay_sec)


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


# Tokens whose CLOB orderbook has been observed 404 ("no orderbook exists for
# the requested token id"). Common for Polymarket weather markets whose makers
# have all cancelled — Gamma still reports a bestBid/bestAsk from the last
# known book, but CLOB returns 404. We cache the dead-book state so the bot's
# poll loop doesn't hammer /book 50× per minute for the same dead token.
_DEAD_BOOK_CACHE: dict[str, float] = {}
_DEAD_BOOK_TTL_SEC = 5 * 60


def get_orderbook(token_id: str) -> dict:
    """Fetch the live order book for a YES token. Used by the fill simulator.

    Returns an empty book on 404 (token has no live orderbook on CLOB — an
    expected condition for low-liquidity weather markets, not an error worth
    logging). Caches dead-book tokens for 5 min to keep poll-loop noise down.
    """
    now_ts = _time.time()
    cached = _DEAD_BOOK_CACHE.get(token_id)
    if cached and (now_ts - cached) < _DEAD_BOOK_TTL_SEC:
        return {"bids": [], "asks": []}
    try:
        resp = _session.get(
            f"{POLYMARKET_CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code == 404:
            _DEAD_BOOK_CACHE[token_id] = now_ts
            return {"bids": [], "asks": []}
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[polymarket] Failed to fetch orderbook for {token_id}: {e}")
        return {"bids": [], "asks": []}


def get_best_bid(token_id: str) -> float | None:
    """Return the highest bid price on the order book, or None if no bids."""
    book = get_orderbook(token_id)
    bids = book.get("bids", [])
    if not bids:
        return None
    try:
        prices = [float(b["price"]) for b in bids if b.get("price")]
        return max(prices) if prices else None
    except (ValueError, KeyError):
        return None


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


# Phase2-05: per-market trade-velocity cache. TTL keeps redundant fetches off
# the gamma /trades endpoint when multiple downstream callers (decision +
# future Phase 3 gate) hit the same market within seconds.
_VELOCITY_CACHE: dict[str, tuple[float, dict]] = {}
_VELOCITY_TTL_SEC = 45.0


def get_trade_velocity(condition_id: str, limit: int = 500) -> dict:
    """Phase2-05: trades-per-minute over the last 30 and 60 minutes for a
    given market. Returns
        {"trades_per_min_30": float, "trades_per_min_60": float, "n_60": int}.
    Empty fetch -> all-zero dict (NOT NULL — zero velocity is a valid datum).

    Cached for `_VELOCITY_TTL_SEC` so repeat callers within the same decision
    pass don't double-fetch. Caller decides whether to use the value (e.g.
    skip if `n_60` is 0 because the API returned nothing).
    """
    import time as _time
    now_ts = _time.time()

    cached = _VELOCITY_CACHE.get(condition_id)
    if cached and (now_ts - cached[0]) < _VELOCITY_TTL_SEC:
        return cached[1]

    trades = get_market_trades(condition_id, limit=limit)

    cutoff_30 = now_ts - 30 * 60
    cutoff_60 = now_ts - 60 * 60
    n_30 = 0
    n_60 = 0
    for t in trades:
        try:
            ts = float(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff_60:
            n_60 += 1
            if ts >= cutoff_30:
                n_30 += 1

    out = {
        "trades_per_min_30": n_30 / 30.0,
        "trades_per_min_60": n_60 / 60.0,
        "n_60":              n_60,
    }
    _VELOCITY_CACHE[condition_id] = (now_ts, out)
    return out


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


def _parse_gamma_market(m: dict) -> dict | None:
    """Parse a Gamma API market object into our standard dict."""
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


def _clob_fallback(market_id: str) -> dict | None:
    """Fallback: CLOB → full token ID → Gamma bulk lookup with event slug."""
    try:
        resp = requests.get(
            f"{POLYMARKET_CLOB_API}/markets/{market_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        clob = resp.json()
        tokens = clob.get("tokens", [])
        yes_tok = next((t for t in tokens if (t.get("outcome") or "").upper() == "YES"), None)
        if not yes_tok:
            return None
        full_token_id = yes_tok["token_id"]

        gamma_resp = _session.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"clob_token_ids": full_token_id},
            timeout=10,
        )
        if gamma_resp.status_code == 200:
            data = gamma_resp.json()
            if isinstance(data, list) and data:
                result = _parse_gamma_market(data[0])
                if result:
                    return result

        # Gamma bulk also failed — build minimal record from CLOB data
        question = clob.get("question", "")
        market_slug = clob.get("market_slug", "")
        return {
            "id":         clob.get("condition_id") or market_id,
            "question":   question,
            "token_id":   full_token_id,
            "price":      None,
            "liquidity":  0,
            "volume":     0,
            "end_date":   clob.get("end_date_iso"),
            "resolved":   clob.get("closed", False),
            "market_url": f"https://polymarket.com/event/{market_slug}" if market_slug else "",
        }
    except Exception:
        return None


def get_market_by_id(market_id: str) -> dict | None:
    """Fetch a single market by condition ID. Used for ensemble and resolver checks."""
    try:
        resp = _session.get(
            f"{POLYMARKET_GAMMA_API}/markets/{market_id}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        m = data[0] if isinstance(data, list) and data else data
        if not m or not isinstance(m, dict):
            return _clob_fallback(market_id)
        result = _parse_gamma_market(m)
        return result if result else _clob_fallback(market_id)
    except Exception:
        return _clob_fallback(market_id)


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


def get_resolution_status(condition_id: str) -> dict | None:
    """
    Check if a market is resolved via the CLOB /markets/ endpoint.

    The CLOB response includes a `tokens` array where each token has a
    `winner` boolean once the market resolves.  Also checks the top-level
    `closed` flag.

    Returns:
        {"resolved": True, "yes_price": 1.0|0.0}  — market resolved
        {"resolved": False}                        — market not yet resolved
        None                                       — API call failed
    """
    try:
        resp = requests.get(
            f"{POLYMARKET_CLOB_API}/markets/{condition_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

        # Check tokens array for winner field
        tokens = data.get("tokens", [])
        for t in tokens:
            outcome = (t.get("outcome") or "").upper()
            winner = t.get("winner")
            if winner is True and outcome == "YES":
                return {"resolved": True, "yes_price": 1.0}
            if winner is True and outcome == "NO":
                return {"resolved": True, "yes_price": 0.0}

        # No winner field set — check if market is at least closed
        if data.get("closed") is True:
            # Market closed but no winner yet (still in UMA resolution window).
            # Try to infer from token prices if available.
            for t in tokens:
                outcome = (t.get("outcome") or "").upper()
                price = t.get("price")
                if price is not None and outcome == "YES":
                    price = float(price)
                    if price >= 0.98:
                        return {"resolved": True, "yes_price": 1.0}
                    elif price <= 0.02:
                        return {"resolved": True, "yes_price": 0.0}
            return {"resolved": False}

        return {"resolved": False}
    except Exception as e:
        print(f"[polymarket] get_resolution_status failed for {condition_id}: {e}")
        return None
