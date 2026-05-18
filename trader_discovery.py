"""
Trader Discovery — one-time (or periodic) bootstrap script.

Scans weather markets on Polymarket, harvests wallet addresses from trade history,
filters to high-activity weather traders, scores them against our resolved trades,
and seeds the tracked_traders table.

Run: python trader_discovery.py
"""

import time
import sqlite3
from datetime import datetime, timezone, timedelta

from config import DB_PATH, WEATHER
from db import init_db, upsert_tracked_trader
from markets.polymarket import get_market_trades, iter_weather_markets

# ── Config (pulled from config.py WEATHER block — edit there to tune) ─────────

MIN_WEATHER_TRADES    = WEATHER.get("trader_discovery_min_trades",    5)
MAX_WEATHER_MARKETS   = WEATHER.get("trader_discovery_max_markets",   400)
MAX_TRADES_PER_MARKET = WEATHER.get("trader_discovery_max_tpm",       5.0)
MAX_AVG_ENTRY_PRICE   = WEATHER.get("trader_discovery_max_avg_price", 0.85)
RECENCY_DAYS          = WEATHER.get("trader_discovery_recency_days",  60)
API_DELAY             = 0.15   # seconds between market trade fetches


# ── Step 1: Collect weather condition_ids ─────────────────────────────────────

def _condition_ids_from_trades_db() -> list[str]:
    """Pull all unique condition_ids from our own trade history."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT market_id FROM trades WHERE market_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


def _condition_ids_from_gamma() -> list[str]:
    """
    Fetch weather market condition IDs via deterministic slug discovery
    (`/events/slug/{slug}`) across the catalog city universe + a wide
    forward window — same data path as the scanner/catalog.

    Name preserved for back-compat though the source is no longer Gamma's
    `/markets` index walk (broken 2026-05-15).
    """
    cities = WEATHER.get("catalog_cities") or WEATHER.get("top_cities", [])
    ids: list[str] = []
    for m in iter_weather_markets(cities, days_ahead=6, request_delay_sec=0.1):
        slug_check = (m.get("slug") or m.get("_event_slug") or "").lower()
        if "highest-temperature" not in slug_check and "lowest-temperature" not in slug_check:
            continue
        cid = m.get("conditionId") or m.get("id")
        if cid:
            ids.append(cid)
    return ids


def get_weather_condition_ids() -> list[str]:
    print("[discovery] Collecting weather market condition IDs...")
    from_db    = _condition_ids_from_trades_db()
    from_gamma = _condition_ids_from_gamma()
    combined   = list({*from_db, *from_gamma})
    print(f"[discovery]   {len(from_db)} from trade history  +  {len(from_gamma)} from Gamma  =  {len(combined)} unique")
    return combined


# ── Step 2: Harvest wallets ───────────────────────────────────────────────────

def harvest_wallets(condition_ids: list[str]) -> dict:
    """
    For each condition_id, fetch trade history and aggregate by wallet.

    Returns:
        {
          wallet: {
            "pseudonym":       str,
            "display_name":    str,
            "markets_traded":  set of condition_ids,
            "trades":          [{"condition_id", "outcome", "price", "side", "timestamp"}],
            "last_active":     int (unix timestamp),
          }
        }
    """
    wallet_map = {}
    total = len(condition_ids)

    print(f"[discovery] Harvesting trades from {total} weather markets...")

    for i, cid in enumerate(condition_ids, 1):
        if i % 50 == 0 or i == total:
            print(f"  {i}/{total}  wallets seen so far: {len(wallet_map)}")

        trades = get_market_trades(cid, limit=500)
        for t in trades:
            w = t.get("wallet", "")
            if not w:
                continue

            if w not in wallet_map:
                wallet_map[w] = {
                    "pseudonym":      t.get("pseudonym", ""),
                    "display_name":   t.get("name", ""),
                    "markets_traded": set(),
                    "trades":         [],
                    "last_active":    0,
                }

            entry = wallet_map[w]
            entry["markets_traded"].add(cid)
            entry["trades"].append({
                "condition_id": cid,
                "token_id":     t.get("token_id", ""),
                "outcome":      t.get("outcome", ""),
                "price":        t.get("price", 0.0),
                "side":         t.get("side", ""),
                "timestamp":    t.get("timestamp", 0),
            })
            if t.get("timestamp", 0) > entry["last_active"]:
                entry["last_active"]  = t["timestamp"]
                entry["pseudonym"]    = t.get("pseudonym", entry["pseudonym"])
                entry["display_name"] = t.get("name", entry["display_name"])

        time.sleep(API_DELAY)

    print(f"[discovery] Done. {len(wallet_map)} unique wallets found across all markets.")
    return wallet_map


# ── Step 3: Filter & score ────────────────────────────────────────────────────

def _filter_qualifying(wallet_map: dict) -> dict:
    """Return subset of wallet_map that passes AMM, HFT, and activity filters."""
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)).timestamp())
    result = {}
    for w, data in wallet_map.items():
        n_unique = len(data["markets_traded"])
        if not (MIN_WEATHER_TRADES <= n_unique <= MAX_WEATHER_MARKETS):
            continue
        if data["last_active"] < cutoff_ts:
            continue
        # HFT filter — genuine forecasters enter 1-3x per market on average
        trades_per_market = len(data["trades"]) / n_unique
        if trades_per_market > MAX_TRADES_PER_MARKET:
            continue
        result[w] = data
    return result


def build_resolution_cache(wallet_map: dict) -> dict:
    """
    For every unique weather market traded by wallets in wallet_map, call the
    CLOB midpoint to determine if it has resolved and which way.

    Uses the token_id and outcome already stored in each trade record so we
    need exactly one CLOB call per unique condition_id — independent of our
    own trade history entirely.

    Returns {condition_id: 'YES' | 'NO'}
    """
    from markets.polymarket import get_midpoint

    # One representative (token_id, outcome) per condition_id
    market_tokens: dict[str, tuple[str, str]] = {}
    for data in wallet_map.values():
        for t in data["trades"]:
            cid = t["condition_id"]
            if cid not in market_tokens and t.get("token_id"):
                market_tokens[cid] = (t["token_id"], t["outcome"])

    print(f"[discovery] Checking resolution for {len(market_tokens)} unique markets via CLOB...")

    resolution_cache: dict[str, str] = {}
    for i, (cid, (token_id, outcome)) in enumerate(market_tokens.items(), 1):
        if i % 100 == 0:
            print(f"  {i}/{len(market_tokens)}  resolved so far: {len(resolution_cache)}")

        mid = get_midpoint(token_id)
        if mid is None:
            time.sleep(API_DELAY)
            continue

        if mid > 0.95:
            # Token settled at ~1.0 — this outcome won
            resolution_cache[cid] = "YES" if outcome.upper() == "YES" else "NO"
        elif mid < 0.05:
            # Token settled at ~0.0 — this outcome lost
            resolution_cache[cid] = "NO" if outcome.upper() == "YES" else "YES"
        # else: still live, skip

        time.sleep(API_DELAY)

    print(f"[discovery] Done. {len(resolution_cache)} resolved out of {len(market_tokens)} checked.")
    return resolution_cache


def score_wallets(wallet_map: dict) -> list[dict]:
    """
    Filter to genuine forecasters, score each wallet's accuracy independently
    against CLOB-confirmed resolutions — no dependency on our own trade history.
    """
    qualifying  = _filter_qualifying(wallet_map)
    print(f"[discovery] {len(qualifying)} wallets passed filters (of {len(wallet_map)} total).")

    resolution_cache = build_resolution_cache(qualifying)
    now_iso = datetime.now(timezone.utc).isoformat()

    qualified = []
    for wallet, data in qualifying.items():
        n_resolved = 0
        n_correct  = 0
        for t in data["trades"]:
            if t.get("side", "").upper() != "BUY":
                continue  # exits don't count as predictions
            actual = resolution_cache.get(t["condition_id"])
            if actual is None:
                continue
            n_resolved += 1
            if t.get("outcome", "").upper() == actual:
                n_correct += 1

        win_rate = (n_correct / n_resolved) if n_resolved > 0 else None

        # Certainty harvester filter — skip wallets that only enter near-certain outcomes
        prices = [t["price"] for t in data["trades"] if t.get("price", 0) > 0]
        avg_price = sum(prices) / len(prices) if prices else 0
        if avg_price > MAX_AVG_ENTRY_PRICE:
            continue

        qualified.append({
            "wallet":           wallet,
            "pseudonym":        data["pseudonym"],
            "display_name":     data["display_name"],
            "n_weather_trades": len(data["trades"]),
            "n_unique_markets": len(data["markets_traded"]),
            "n_resolved":       n_resolved,
            "win_rate":         win_rate,
            "last_active":      data["last_active"],
            "discovered_at":    now_iso,
        })

    qualified.sort(key=lambda x: (-(x["n_resolved"]), -(x["n_unique_markets"])))
    return qualified


# ── Step 4: Save & report ─────────────────────────────────────────────────────

def save_and_report(scored: list[dict]):
    if not scored:
        print("\nNo wallets met the minimum criteria.")
        return

    for trader in scored:
        upsert_tracked_trader(trader)

    print(f"[discovery] {'='*60}")
    print(f"[discovery] RESULTS — {len(scored)} trader(s) upserted into tracked_traders")
    print(f"[discovery] {'='*60}")
    print(f"  {'WALLET':<44}  {'PSEUDONYM':<22}  {'MKTS':>4}  {'RESOLVD':>7}  {'WIN%':>6}")
    print(f"  {'-'*44}  {'-'*22}  {'-'*4}  {'-'*7}  {'-'*6}")

    for t in scored[:30]:
        win_pct = f"{t['win_rate']*100:.0f}%" if t["win_rate"] is not None else "  n/a"
        print(
            f"  {t['wallet']:<44}  "
            f"{(t['pseudonym'] or t['display_name'])[:22]:<22}  "
            f"{t['n_unique_markets']:>4}  "
            f"{t['n_resolved']:>7}  "
            f"{win_pct:>6}"
        )

    if len(scored) > 30:
        print(f"[discovery]   ... and {len(scored) - 30} more")

    scored_with_rate = [t for t in scored if t["win_rate"] is not None]
    print(f"[discovery] {len(scored_with_rate)} trader(s) have resolved trade data for accuracy scoring.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  POLYMARKET TRADER DISCOVERY")
    print("=" * 70)

    init_db()

    condition_ids = get_weather_condition_ids()
    if not condition_ids:
        print("No condition IDs found. Ensure the bot has run at least once.")
        return

    wallet_map = harvest_wallets(condition_ids)
    scored     = score_wallets(wallet_map)
    save_and_report(scored)


if __name__ == "__main__":
    main()
