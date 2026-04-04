"""
Weather Calibration Probe
--------------------------
Exploratory script to find which Polymarket API strategy can retrieve
actual resolution outcomes (YES/NO) for markets that are already closed.

Tests four strategies against closed early-exit trades from the DB:
  1. GET /markets/{market_id}                        — path endpoint (422 on closed)
  2. GET /markets?slug={event_slug}                  — slug query param
  3. GET /markets?closed=true ordered by end_date    — bulk closed, recent-first, paginated
  4. GET /clob/midpoint?token_id={token_id}          — CLOB midpoint on stored YES token

Run:  python weather_calibration_probe.py
No writes. Prints a comparison table + raw dump for any strategy that succeeds.
"""
import json
import re
import sys
import requests
from datetime import datetime, timezone

import db
from config import POLYMARKET_GAMMA_API, POLYMARKET_CLOB_API

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-bot/1.0"})

_YES_THRESHOLD = 0.98
_NO_THRESHOLD  = 0.02


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_slug(market_url: str) -> str | None:
    m = re.search(r"/event/([^/?#]+)", market_url or "")
    return m.group(1) if m else None


def _interpret_price(yes_price: float | None) -> str:
    if yes_price is None:
        return "no price"
    if yes_price >= _YES_THRESHOLD:
        return f"RESOLVED YES  ({yes_price:.3f})"
    if yes_price <= _NO_THRESHOLD:
        return f"RESOLVED NO   ({yes_price:.3f})"
    return f"unresolved    ({yes_price:.3f})"


def _yes_price_from_market(m: dict) -> float | None:
    def parse(v):
        if isinstance(v, list):
            return v
        try:
            return json.loads(v)
        except Exception:
            return []

    outcomes = parse(m.get("outcomes", "[]"))
    prices   = parse(m.get("outcomePrices", "[]"))
    yes_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None)
    if yes_idx is None or yes_idx >= len(prices):
        return None
    try:
        return float(prices[yes_idx])
    except Exception:
        return None


def _market_slug(m: dict) -> str:
    events     = m.get("events") or []
    event_slug = events[0].get("slug") if events else None
    return event_slug or m.get("slug") or ""


# ── Strategy 1: path endpoint /markets/{id} ───────────────────────────────────

def strategy_path(market_id: str) -> tuple[str, float | None]:
    try:
        resp = _session.get(f"{POLYMARKET_GAMMA_API}/markets/{market_id}", timeout=10)
        if resp.status_code == 422:
            return "422", None
        resp.raise_for_status()
        data = resp.json()
        m    = data[0] if isinstance(data, list) and data else data
        if not m or not isinstance(m, dict):
            return "empty", None
        return f"ok closed={m.get('closed')}", _yes_price_from_market(m)
    except Exception as e:
        return f"err:{e}", None


# ── Strategy 2: ?slug= query ──────────────────────────────────────────────────

def strategy_slug_param(slug: str) -> tuple[str, float | None]:
    if not slug:
        return "no slug", None
    try:
        resp = _session.get(f"{POLYMARKET_GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else []
        if not markets:
            return "empty", None
        m = markets[0]
        return f"ok n={len(markets)} closed={m.get('closed')}", _yes_price_from_market(m)
    except Exception as e:
        return f"err:{e}", None


# ── Strategy 3: bulk closed=true, recent-first, search by slug ───────────────

_closed_weather: dict[str, dict] = {}   # slug → market object, populated once

def _load_closed_weather_markets(max_pages: int = 10):
    """
    Fetch recently-closed markets ordered by end_date descending,
    keep only ones that look like weather markets. Stops early if
    end_date falls more than 14 days ago.
    """
    global _closed_weather
    cutoff = datetime.now(timezone.utc).timestamp() - 14 * 86400
    found  = 0

    for page in range(max_pages):
        offset = page * 200
        print(f"  [strategy3] Fetching closed page {page+1} (offset={offset})...")
        try:
            resp = _session.get(
                f"{POLYMARKET_GAMMA_API}/markets",
                params={
                    "closed":     "true",
                    "limit":      200,
                    "offset":     offset,
                    "order":      "end_date_iso",
                    "ascending":  "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                print(f"  [strategy3] Empty batch at page {page+1}, stopping.")
                break
        except Exception as e:
            print(f"  [strategy3] Error on page {page+1}: {e}")
            break

        stale = 0
        for m in batch:
            # Check end_date to stop iterating old history
            ed_str = m.get("endDateIso") or m.get("endDate") or ""
            if ed_str:
                try:
                    ed = datetime.fromisoformat(ed_str.replace("Z", "+00:00"))
                    if ed.timestamp() < cutoff:
                        stale += 1
                except Exception:
                    pass

            slug = _market_slug(m)
            if "highest-temperature" in slug.lower():
                _closed_weather[slug] = m
                found += 1

        print(f"  [strategy3] Page {page+1}: {len(batch)} markets, {found} weather hits so far")

        # Stop if most of the batch is stale (older than our window)
        if stale > len(batch) * 0.8:
            print("  [strategy3] Mostly stale results, stopping pagination.")
            break

    print(f"  [strategy3] Done. Loaded {len(_closed_weather)} closed weather markets.\n")


def strategy_closed_bulk(slug: str, market_id: str) -> tuple[str, float | None]:
    if not _closed_weather:
        _load_closed_weather_markets()

    # Try exact match, then partial match
    m = _closed_weather.get(slug)
    if not m:
        for k, v in _closed_weather.items():
            if slug and slug in k:
                m = v
                break
    if not m:
        # Last resort: match by conditionId
        for v in _closed_weather.values():
            if (v.get("conditionId") or v.get("id")) == market_id:
                m = v
                break

    if not m:
        return f"not found ({len(_closed_weather)} loaded)", None

    return f"found closed={m.get('closed')}", _yes_price_from_market(m)


# ── Strategy 4: CLOB midpoint on stored YES token_id ────────────────────────

def strategy_clob_midpoint(token_id: str) -> tuple[str, float | None]:
    """
    Use the CLOB /midpoint endpoint with the YES token_id stored at trade entry.
    For resolved YES markets: YES token price → ~1.0
    For resolved NO  markets: YES token price → ~0.0
    """
    if not token_id:
        return "no token_id", None
    try:
        resp = _session.get(
            f"{POLYMARKET_CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code == 404:
            return "404 not found", None
        resp.raise_for_status()
        data = resp.json()
        mid  = data.get("mid")
        if mid is None:
            return f"no mid field: {data}", None
        return "ok", float(mid)
    except Exception as e:
        return f"err:{e}", None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    all_trades  = db.get_all_trades()
    early_exits = [
        t for t in all_trades
        if t.get("status") == "closed"
        and t.get("exit_reason")
        and "resolved" not in (t.get("exit_reason") or "").lower()
    ]

    if not early_exits:
        print("No early-exit closed trades found in DB.")
        sys.exit(0)

    now = datetime.now(timezone.utc)

    def sort_key(t):
        try:
            return datetime.fromisoformat((t.get("end_date") or "").replace("Z", "+00:00"))
        except Exception:
            return datetime.max.replace(tzinfo=timezone.utc)

    early_exits.sort(key=sort_key)

    print(f"\nFound {len(early_exits)} early-exit trades. Testing up to 8.\n")
    trades_to_test = early_exits[:8]

    # Print summary of trades being tested
    print(f"{'City':<14} {'Dir':<4} {'EndDate':<12} {'token_id present':<18} {'Exit Reason'}")
    print("-" * 85)
    for t in trades_to_test:
        has_token = "YES" if t.get("token_id") else "NO  ← missing"
        print(f"  {(t.get('city') or '?'):<12} {t.get('direction','?'):<4} "
              f"{(t.get('end_date') or '?')[:10]:<12} {has_token:<18} "
              f"{(t.get('exit_reason') or '?')[:40]}")
    print()

    # Pre-load Strategy 3 cache once (avoids re-fetching per trade)
    print("Pre-loading Strategy 3 (closed bulk)...")
    _load_closed_weather_markets()

    # Results table
    col = 36
    print("=" * (16 + col * 4))
    print(f"{'Trade':<16} {'S1: /markets/id':<{col}} {'S2: ?slug=':<{col}} "
          f"{'S3: closed bulk':<{col}} {'S4: CLOB midpoint':<{col}}")
    print("=" * (16 + col * 4))

    winners = []
    for trade in trades_to_test:
        market_id  = trade.get("market_id", "")
        token_id   = trade.get("token_id", "")
        market_url = trade.get("market_url", "")
        slug       = _extract_slug(market_url) or ""
        city       = (trade.get("city") or "?")[:7]
        direction  = trade.get("direction", "?")
        label      = f"{city} {direction}"[:14]

        s1s, s1p = strategy_path(market_id)
        s2s, s2p = strategy_slug_param(slug)
        s3s, s3p = strategy_closed_bulk(slug, market_id)
        s4s, s4p = strategy_clob_midpoint(token_id)

        def cell(status, price):
            interp = _interpret_price(price)
            # Truncate to fit column
            combined = f"{interp} [{status}]"
            return combined[:col-1]

        print(f"{label:<16} {cell(s1s,s1p):<{col}} {cell(s2s,s2p):<{col}} "
              f"{cell(s3s,s3p):<{col}} {cell(s4s,s4p):<{col}}")

        for strat, (status, price) in [("S4:CLOB", (s4s, s4p)), ("S3:closed", (s3s, s3p)),
                                        ("S2:slug", (s2s, s2p)), ("S1:path", (s1s, s1p))]:
            if price is not None and (price >= _YES_THRESHOLD or price <= _NO_THRESHOLD):
                winners.append((strat, trade, price))
                break

    print()
    if winners:
        print(f"SUCCESS — {len(winners)} trade(s) have retrievable resolution data:")
        for strat, t, price in winners:
            outcome = "YES" if price >= _YES_THRESHOLD else "NO"
            direction = t.get("direction", "?")
            correct = (direction == "YES" and outcome == "YES") or (direction == "NO" and outcome == "NO")
            print(f"  {strat}  {(t.get('city') or '?'):<14} dir={direction} actual={outcome} "
                  f"correct={'✓ WIN' if correct else '✗ LOSS'} (fill={t.get('fill_price',0):.3f})")
        print()
        print("Recommended strategy for calibration backfill: use the first working strategy above.")
    else:
        print("No strategy retrieved resolution data for these trades.")
        print("Possible reasons:")
        print("  - Markets not yet settled by Polymarket")
        print("  - CLOB doesn't serve closed-market token prices")
        print("  - Gamma closed=true endpoint doesn't include weather category")
        print()
        print("Try re-running in a few hours after end_date has passed and Polymarket settles.")

    # Raw dump for debugging the first trade
    print("\n── Raw API dump for first trade (Strategy 4 / CLOB) ──")
    t0 = trades_to_test[0]
    tid = t0.get("token_id", "")
    print(f"  token_id : {tid!r}")
    print(f"  market_id: {t0.get('market_id','')!r}")
    print(f"  slug     : {_extract_slug(t0.get('market_url',''))!r}")
    if tid:
        try:
            r = _session.get(f"{POLYMARKET_CLOB_API}/midpoint", params={"token_id": tid}, timeout=10)
            print(f"  CLOB status: {r.status_code}")
            print(f"  CLOB body  : {r.text[:300]}")
        except Exception as e:
            print(f"  CLOB error : {e}")
    else:
        print("  (no token_id to probe)")


if __name__ == "__main__":
    main()
