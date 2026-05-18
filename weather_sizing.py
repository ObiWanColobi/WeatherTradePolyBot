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


# -- Config (read at call time so live overrides take effect) -----------------
_KELLY_FRACTION         = WEATHER.get("kelly_fraction",                 0.50)   # half-Kelly
_MIN_BET_USDC           = WEATHER.get("kelly_min_bet_usdc",             5.00)   # ignore sub-threshold signals
_MAX_BALANCE_PCT        = WEATHER.get("kelly_max_balance_pct",          0.10)   # never risk >10% of balance

# Forecast horizon discount -- forecast reliability degrades with days to resolution
_HORIZON_DISCOUNTS = {0: 1.00, 1: 0.85, 2: 0.65}
_HORIZON_DISCOUNT_DEFAULT = 0.45   # 3+ days out


def kelly_size_with_diagnostics(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
    ensemble_margin_c:  float | None = None,
) -> tuple[float, dict]:
    """
    Single-pass Kelly sizing that returns (size, diagnostics).

    The diagnostics dict captures every intermediate value and identifies the
    binding constraint, so downstream callers can persist a complete record of
    the decision instead of having to back-derive it from float arithmetic.

    Returns:
        (size_usdc, diagnostics)
        size_usdc is 0.0 when no edge exists or the result falls below the
        minimum bet threshold; diagnostics still describe how that conclusion
        was reached (binding_constraint = "no_edge" or "min_bet_floor").
    """
    if direction.lower() == "no":
        p     = 1.0 - model_prob
        price = 1.0 - market_price
    else:
        p     = model_prob
        price = market_price

    price = max(0.01, min(0.99, price))
    p     = max(0.01, min(0.99, p))

    edge = p - price
    odds = (1.0 - price) / price

    # Read live caps once so the snapshot is internally consistent
    max_bet           = WEATHER.get("kelly_max_bet_usdc",           200.00)
    max_bet_unanimous = WEATHER.get("kelly_max_bet_usdc_unanimous",  50.00)
    cap_per_bet       = max_bet_unanimous if unanimous else max_bet
    cap_balance_pct   = balance * _MAX_BALANCE_PCT

    diag: dict = {
        "balance":             balance,
        "model_prob":          model_prob,
        "market_price":        market_price,
        "p_used":              p,
        "price_used":          price,
        "ensemble_n":          ensemble_n,
        "days_to_resolution":  days_to_resolution,
        "ensemble_margin_c":   ensemble_margin_c,
        "is_unanimous":        1 if unanimous else 0,
        "edge":                edge,
        "odds":                odds,
        "kelly_raw":           0.0,
        "ensemble_scale":      1.0,
        "horizon_mult":        _HORIZON_DISCOUNTS.get(days_to_resolution, _HORIZON_DISCOUNT_DEFAULT),
        "margin_mult":         1.0,
        "kelly_fraction":      _KELLY_FRACTION,
        "kelly_final":         0.0,
        "size_pre_cap":        0.0,
        "cap_per_bet":         cap_per_bet,
        "cap_balance_pct":     cap_balance_pct,
        "size_after_caps":     0.0,
        "binding_constraint":  "no_edge",
    }

    if edge <= 0:
        return 0.0, diag

    kelly = edge / odds
    diag["kelly_raw"] = kelly

    if ensemble_n > 0 and ensemble_n < 30:
        diag["ensemble_scale"] = ensemble_n / 30
        kelly *= diag["ensemble_scale"]

    kelly *= diag["horizon_mult"]

    if ensemble_margin_c is not None:
        diag["margin_mult"] = min(abs(ensemble_margin_c) / 5.0, 1.0)
        kelly *= diag["margin_mult"]

    kelly *= _KELLY_FRACTION
    diag["kelly_final"]  = kelly
    diag["size_pre_cap"] = kelly * balance

    size = diag["size_pre_cap"]
    binding = "kelly"

    if size > cap_per_bet:
        size    = cap_per_bet
        binding = "cap_per_bet"
    if size > cap_balance_pct:
        size    = cap_balance_pct
        binding = "cap_balance_pct"
    size = max(size, 0.0)

    diag["size_after_caps"]    = round(size, 2)
    diag["binding_constraint"] = binding

    # Platt A/B half-size gate: when platt_enabled+platt_half_size are both on,
    # halve the post-cap stake. This is the temporary risk-reducer during the
    # 2-week A/B period. Set platt_half_size=false in env to ramp to full size.
    half_size_applied = bool(WEATHER.get("platt_enabled")) and bool(WEATHER.get("platt_half_size"))
    if half_size_applied:
        size *= 0.5
        diag["binding_constraint"] = "platt_half_size"
    diag["platt_half_size_applied"] = half_size_applied

    if size < _MIN_BET_USDC:
        diag["binding_constraint"] = "min_bet_floor"
        return 0.0, diag

    return round(size, 2), diag


def kelly_size(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
    ensemble_margin_c:  float | None = None,
) -> float:
    """Backwards-compatible wrapper. Returns only the final size."""
    size, _ = kelly_size_with_diagnostics(
        balance, model_prob, market_price, direction,
        ensemble_n, days_to_resolution, unanimous, ensemble_margin_c,
    )
    return size


def size_summary(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
    ensemble_margin_c:  float | None = None,
) -> dict:
    """Return a dict of sizing diagnostics for logging/display."""
    size, diag = kelly_size_with_diagnostics(
        balance, model_prob, market_price, direction,
        ensemble_n, days_to_resolution, unanimous, ensemble_margin_c,
    )
    return {
        **diag,
        "direction": direction,
        "size_usdc": size,
        "max_bet":   diag["cap_per_bet"],
    }
