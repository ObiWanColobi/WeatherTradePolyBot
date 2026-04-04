"""
Trader Monitor — live position polling for tracked traders.

Two public functions called by the bot:

    update(weather_condition_ids)  — poll tracked wallets, snapshot positions
    get_consensus(market_id, direction)  — query consensus for entry gate

Runs every 10 polls (~10 min). Only polls traders with enough resolved history
to be meaningful (MIN_RESOLVED threshold).
"""

import time
import logging
from datetime import datetime, timezone

from config import WEATHER
from db import get_tracked_traders, upsert_trader_positions, get_trader_consensus
from markets.polymarket import get_wallet_positions

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MIN_RESOLVED  = WEATHER.get("trader_consensus_min_resolved", 15)
MAX_WALLETS   = WEATHER.get("trader_monitor_max_wallets",   100)
API_DELAY     = 0.15  # seconds between wallet position fetches


# ── Public API ────────────────────────────────────────────────────────────────

def update(weather_condition_ids: set):
    """
    Poll live positions for qualified tracked traders and snapshot to DB.
    Filters positions to weather markets only using weather_condition_ids.

    Call this from the bot loop every N polls.
    """
    traders = _get_qualified_traders()
    if not traders:
        log.debug("[trader_monitor] No qualified traders to poll.")
        return

    log.info(f"[trader_monitor] Polling {len(traders)} tracked traders for live positions...")

    updated = 0
    for trader in traders:
        wallet = trader["wallet"]
        try:
            raw_positions = get_wallet_positions(wallet)
        except Exception as e:
            log.warning(f"[trader_monitor] Position fetch failed for {wallet}: {e}")
            time.sleep(API_DELAY)
            continue

        # Filter to weather markets only
        weather_positions = [
            p for p in raw_positions
            if p.get("market_id") in weather_condition_ids
            and p.get("size", 0) > 0.01
        ]

        upsert_trader_positions(wallet, weather_positions)
        updated += 1
        time.sleep(API_DELAY)

    log.info(f"[trader_monitor] Positions updated for {updated}/{len(traders)} traders.")


def get_consensus(market_id: str, direction: str) -> dict:
    """
    Return trader consensus for a market/direction pair.

    Result: {"same": int, "opposite": int, "signal": "CONFIRM"|"DIVERGE"|"NEUTRAL"}
    """
    min_resolved = WEATHER.get("trader_consensus_min_resolved", 15)
    min_win_rate = WEATHER.get("trader_consensus_min_win_rate", 0.55)
    return get_trader_consensus(market_id, direction, min_resolved=min_resolved, min_win_rate=min_win_rate)


# ── Internal ──────────────────────────────────────────────────────────────────

def _get_qualified_traders() -> list[dict]:
    """
    Return active tracked traders with enough resolved history to be meaningful,
    sorted by n_resolved descending, capped at MAX_WALLETS.
    """
    all_traders = get_tracked_traders(active_only=True)
    qualified = [t for t in all_traders if (t["n_resolved"] or 0) >= MIN_RESOLVED]
    qualified.sort(key=lambda t: -(t["n_resolved"] or 0))
    return qualified[:MAX_WALLETS]
