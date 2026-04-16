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
from markets.polymarket import get_midpoint, get_market_by_id

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

        if now < end:
            continue   # not expired yet

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
            # CLOB midpoint unavailable (orderbook torn down). Fall back to
            # Gamma/CLOB API to check if market is resolved with outcome prices.
            market_info = get_market_by_id(trade.get("market_id", ""))
            if market_info and market_info.get("resolved"):
                yes_price = market_info.get("price")  # Gamma YES price
            if yes_price is None:
                print(f"  [resolve] CLOB midpoint unavailable for {trade['market_name'][:50]} — skipping")
                continue

        # Only settle if price has actually resolved to near-binary
        if yes_price >= _RESOLVED_YES_THRESHOLD:
            resolved_yes = True
        elif yes_price <= _RESOLVED_NO_THRESHOLD:
            resolved_yes = False
        else:
            # Market expired but price hasn't settled — Polymarket still processing
            print(f"  [resolve] Waiting on settlement: {trade['market_name'][:50]} YES={yes_price:.2f}")
            continue

        executor.settle_resolved(trade, resolved_yes)
        settled += 1

    return settled
