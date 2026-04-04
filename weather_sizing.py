"""
Weather Position Sizing
------------------------
Kelly criterion sizing for binary weather markets, clamped to a
configurable maximum bet.

Kelly formula for binary market:
    edge  = p - price          (if BUY YES, p = model P(YES))
    odds  = (1 - price) / price  (payout per dollar risked)
    kelly = edge / odds * balance

Where:
    p     = ensemble probability (e.g. 0.82 from 57/69 members)
    price = current YES market price (e.g. $0.30)

We use fractional Kelly (default 0.5) to reduce variance, then clamp
to a hard maximum to prevent any single bet from being too large.

The ensemble probability is preferred over the blended layer probability
because it has a direct physical interpretation: fraction of model runs
that resolve YES. When ensemble data is unavailable (< 10 members) the
sigmoid-derived probability is used as a fallback at reduced confidence.

Forecast horizon discount:
    Polymarket posts markets 3-4 days in advance. Forecast reliability
    degrades significantly beyond 24 hours. Kelly is discounted by a
    multiplier based on days until resolution:
        0 days (same-day)  -> 1.00x  (most reliable)
        1 day out          -> 0.85x
        2 days out         -> 0.65x
        3+ days out        -> 0.45x  (least reliable)
"""
from config import WEATHER


# -- Config -------------------------------------------------------------------
_KELLY_FRACTION  = WEATHER.get("kelly_fraction",        0.50)   # half-Kelly
_MAX_BET_USDC    = WEATHER.get("kelly_max_bet_usdc",   50.00)   # hard cap per trade
_MIN_BET_USDC    = WEATHER.get("kelly_min_bet_usdc",    5.00)   # ignore sub-threshold signals
_MAX_BALANCE_PCT = WEATHER.get("kelly_max_balance_pct", 0.10)   # never risk >10% of balance

# Forecast horizon discount -- forecast reliability degrades with days to resolution
_HORIZON_DISCOUNTS = {0: 1.00, 1: 0.85, 2: 0.65}
_HORIZON_DISCOUNT_DEFAULT = 0.45   # 3+ days out


def kelly_size(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
) -> float:
    """
    Calculate position size in USDC using fractional Kelly, clamped.

    Args:
        balance:            current paper/live balance in USDC
        model_prob:         P(YES) from ensemble (0.0 - 1.0)
        market_price:       current YES price on Polymarket (0.0 - 1.0)
        direction:          "yes" or "no"
        ensemble_n:         number of ensemble members used (lower = less confident)
        days_to_resolution: whole days until market resolves (0 = same-day, 1 = tomorrow, ...)

    Returns:
        size in USDC, or 0.0 if Kelly is negative (no edge).
    """
    # For BUY NO we invert: p = P(NO) = 1 - model_prob, price = 1 - market_price
    if direction.lower() == "no":
        p     = 1.0 - model_prob
        price = 1.0 - market_price
    else:
        p     = model_prob
        price = market_price

    # Clamp to avoid degenerate inputs
    price = max(0.01, min(0.99, price))
    p     = max(0.01, min(0.99, p))

    edge = p - price
    if edge <= 0:
        return 0.0   # negative Kelly -- no edge, don't bet

    odds  = (1.0 - price) / price   # profit per dollar staked if we win
    kelly = edge / odds              # fraction of bankroll to bet

    # Scale down if ensemble is small (< 30 members = less conviction)
    if ensemble_n > 0 and ensemble_n < 30:
        kelly *= ensemble_n / 30

    # Forecast horizon discount -- further out = less reliable forecast
    horizon_mult = _HORIZON_DISCOUNTS.get(days_to_resolution, _HORIZON_DISCOUNT_DEFAULT)
    kelly *= horizon_mult

    # Apply fractional Kelly and balance cap
    fraction = kelly * _KELLY_FRACTION
    size     = fraction * balance

    # Hard caps
    size = min(size, _MAX_BET_USDC)
    size = min(size, balance * _MAX_BALANCE_PCT)
    size = max(size, 0.0)

    # Drop sub-threshold bets -- not worth the slippage
    if size < _MIN_BET_USDC:
        return 0.0

    return round(size, 2)


def size_summary(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
) -> dict:
    """
    Return a dict of sizing diagnostics for logging/display.
    """
    if direction.lower() == "no":
        p     = 1.0 - model_prob
        price = 1.0 - market_price
    else:
        p     = model_prob
        price = market_price

    price = max(0.01, min(0.99, price))
    p     = max(0.01, min(0.99, p))
    edge  = p - price
    odds  = (1.0 - price) / price if price > 0 else 0
    kelly = (edge / odds) if odds > 0 and edge > 0 else 0.0
    horizon_mult = _HORIZON_DISCOUNTS.get(days_to_resolution, _HORIZON_DISCOUNT_DEFAULT)

    size = kelly_size(balance, model_prob, market_price, direction, ensemble_n, days_to_resolution)

    return {
        "direction":       direction,
        "model_prob":      p,
        "market_price":    price,
        "edge":            edge,
        "odds":            odds,
        "kelly_raw":       kelly,
        "horizon_mult":    horizon_mult,
        "kelly_frac":      kelly * horizon_mult * _KELLY_FRACTION,
        "size_usdc":       size,
        "balance":         balance,
        "max_bet":         _MAX_BET_USDC,
        "days_to_resolve": days_to_resolution,
    }
