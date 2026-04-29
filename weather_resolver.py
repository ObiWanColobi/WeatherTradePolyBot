"""
Weather Market Resolver
------------------------
Detects open paper trades whose markets have expired and settles them
at the correct resolution price.

Polymarket weather markets resolve to YES=1.00 or YES=0.00 once the
outcome is known. The resolver polls open trades each bot cycle, checks
whether the end_date has passed AND whether the market price has settled
to a near-binary value, then closes the trade via settle_resolved().

Skips trades where:
  - end_date has not yet passed
  - price is still in mid-range (market settling, not yet final)
  - market data cannot be fetched

Trades are kept in the DB as a permanent log — resolved trades are
closed (status='closed') but never deleted.
"""
from datetime import datetime, timezone

import db
from markets.polymarket import get_midpoint, get_market_by_id, get_resolution_status
from notifications import notify

# YES price thresholds to confirm resolution has occurred
_RESOLVED_YES_THRESHOLD = 0.98   # above this = resolved YES
_RESOLVED_NO_THRESHOLD  = 0.02   # below this = resolved NO


def run_resolve_pass(executor) -> int:
    """
    Check all open trades for resolution. Settle any that have expired
    and whose CLOB midpoint confirms the outcome.

    Uses the CLOB midpoint API (via stored YES token_id) instead of the
    Gamma API, which returns 422 for closed/resolved markets.

    Args:
        executor: PaperExecutor instance (provides settle_resolved)

    Returns:
        Number of trades settled this pass.
    """
    open_trades = db.get_open_trades()
    if not open_trades:
        return 0

    now     = datetime.now(timezone.utc)
    settled = 0

    for trade in open_trades:
        end_date_str = trade.get("end_date", "")
        if not end_date_str:
            continue

        # Parse end_date — treat naive datetimes as end-of-day UTC
        try:
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except Exception:
            continue

        # Polymarket resolves weather markets early once METAR locks in the
        # day's high. Allow the resolution check to run within the final
        # 12h before nominal close so we don't sit on resolved positions
        # while their token vanishes from the orderbook.
        hours_to_end = (end - now).total_seconds() / 3600
        if hours_to_end > 12:
            continue   # too early — Polymarket can't resolve this far out

        # Use CLOB midpoint on the YES token to check resolution.
        # The Gamma API returns 422 for closed markets, but CLOB midpoint
        # reliably returns ~0.999 (YES) or ~0.001 (NO) for resolved markets.
        token_id = trade.get("token_id")
        if not token_id:
            print(f"  [resolve] No token_id for {trade['market_name'][:50]} — skipping")
            continue

        # For NO trades, token_id is the NO token. We need the YES token
        # to determine YES/NO resolution. The YES midpoint tells us the outcome.
        # However, we can also resolve from the NO token: if NO token mid ~0.999
        # then resolved NO (our token won); if ~0.001 then resolved YES (our token lost).
        yes_price = get_midpoint(token_id)

        direction = (trade.get("direction") or "YES").upper()

        if yes_price is not None:
            # For NO trades, token_id is the NO token, so midpoint is the NO price.
            # Convert to YES price for consistent resolution logic.
            if direction == "NO":
                yes_price = 1.0 - yes_price
        else:
            # CLOB midpoint unavailable (orderbook torn down). Try multiple
            # fallbacks to determine resolution status.
            market_id = trade.get("market_id", "")

            # Fallback 1: CLOB /markets/ endpoint — has token `winner` field
            # and token prices even after orderbook is torn down.
            resolution = get_resolution_status(market_id)
            if resolution and resolution.get("resolved"):
                yes_price = resolution["yes_price"]

            # Fallback 2: Gamma API — has outcomePrices and closed flag
            if yes_price is None:
                market_info = get_market_by_id(market_id)
                if market_info and market_info.get("resolved"):
                    yes_price = market_info.get("price")

            if yes_price is None:
                # All sources failed — alert if market is very stale (>6h past close)
                hours_past = (now - end).total_seconds() / 3600
                if hours_past > 6:
                    _alert_stale_unresolved(trade, hours_past)
                elif hours_past >= 0:
                    print(f"  [resolve] CLOB midpoint unavailable for "
                          f"{trade['market_name'][:50]} — skipping")
                # Pre-close (early polling) — silent skip
                continue

        # Only settle if price has actually resolved to near-binary
        if yes_price >= _RESOLVED_YES_THRESHOLD:
            resolved_yes = True
        elif yes_price <= _RESOLVED_NO_THRESHOLD:
            resolved_yes = False
        else:
            # Price hasn't settled yet. Only log when past nominal close to
            # avoid spamming during the early-resolution polling window.
            if hours_to_end <= 0:
                print(f"  [resolve] Waiting on settlement: {trade['market_name'][:50]} YES={yes_price:.2f}")
            continue

        executor.settle_resolved(trade, resolved_yes)
        settled += 1

    return settled


# Track which trades have already sent a stale alert to avoid spam
_stale_alerted: set[int] = set()


def _alert_stale_unresolved(trade: dict, hours_past: float):
    """Send a one-time Discord alert for a trade stuck unresolved >6h past close."""
    trade_id = trade["id"]
    if trade_id in _stale_alerted:
        return
    _stale_alerted.add(trade_id)
    name = trade.get("market_name", "unknown")[:60]
    notify(
        "warning",
        "Stale Unresolved Trade",
        f"Trade #{trade_id} is {hours_past:.0f}h past market close "
        f"and no API source confirms resolution.",
        fields={
            "Market": name,
            "Direction": trade.get("direction", "?"),
            "Hours past close": f"{hours_past:.1f}h",
        },
    )
    print(f"  [resolve] ALERT: {name} is {hours_past:.0f}h past close — "
          f"no resolution detected from any source")
