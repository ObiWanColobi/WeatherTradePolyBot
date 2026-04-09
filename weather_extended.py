"""
Extended Positions Pass
────────────────────────
Evaluates open positions for scale-in add-on legs. Runs after entry pass,
before exit pass. Skipped entirely when extended_positions_enabled is False.

Eligibility (all must pass):
  1. Leg cap      — position hasn't reached max legs
  2. Time band    — hours_to_close is within the correct band for this add-on
  3. Cooldown     — minimum hours since last leg
  4. Ens ratchet  — ensemble conviction equal or stronger than previous leg
  5. Kelly sizing — fresh Kelly with current inputs
  6. Exposure cap — total portfolio exposure within limit
"""
from datetime import datetime, timezone

import db
from config import WEATHER
from weather_sizing import kelly_size

_ENABLED        = WEATHER.get("extended_positions_enabled", True)
_MAX_ADD_ONS    = WEATHER.get("extended_positions_max_add_ons", 2)
_LEG_SPACING_H  = WEATHER.get("extended_positions_leg_spacing_hours", 12.0)
_MIN_COOLDOWN_H = WEATHER.get("extended_positions_min_cooldown_hours", 12.0)
_MAX_EXPOSURE   = WEATHER.get("decision_max_exposure_pct", 0.90)


def check_extended_position(trade: dict, current_scan: dict | None) -> dict | None:
    """
    Evaluate whether an open parent trade qualifies for an add-on leg.

    Args:
        trade:         open parent trade row from DB (parent_trade_id IS NULL)
        current_scan:  fresh scan result for this market from the weather layer,
                       or None if scan unavailable. Must contain:
                       - ens_yes, ens_n, ens_pct (current ensemble)
                       - model_prob, market_price (for Kelly)
                       - hours_to_close, days_to_resolution

    Returns:
        dict with add-on details if eligible, or None if not eligible.
        Dict keys: size_usdc, direction, model_prob, market_price, ens_yes, ens_n,
                   ens_pct, leg_number, parent_trade_id, hours_to_close, days_to_resolution
    """
    if not _ENABLED:
        return None

    if current_scan is None:
        return None

    parent_id = trade["id"]
    direction = trade["direction"].upper()

    # 1. Leg cap
    leg_count = db.get_leg_count(parent_id)
    if leg_count >= _MAX_ADD_ONS + 1:
        return None

    # 2. Time band
    hours_to_close = current_scan.get("hours_to_close")
    if hours_to_close is None:
        return None

    add_on_index = leg_count  # 0-indexed: leg_count=1 means this is add-on #1
    required_hours = (_MAX_ADD_ONS - add_on_index + 1) * _LEG_SPACING_H
    if hours_to_close >= required_hours:
        return None

    # 3. Cooldown
    latest_leg = db.get_latest_leg(parent_id)
    if latest_leg:
        hours_since = _hours_since(latest_leg.get("opened_at", ""))
        if hours_since is not None and hours_since < _MIN_COOLDOWN_H:
            return None

    # 4. Ensemble ratchet — conviction must be equal or stronger than previous leg
    cur_ens_yes = current_scan.get("ens_yes")
    cur_ens_n   = current_scan.get("ens_n")
    if cur_ens_yes is None or not cur_ens_n:
        return None

    prev_ens_yes = latest_leg.get("entry_ensemble_yes") if latest_leg else None
    prev_ens_n   = latest_leg.get("entry_ensemble_n") if latest_leg else None
    if prev_ens_yes is None or not prev_ens_n:
        return None

    cur_ratio  = cur_ens_yes / cur_ens_n
    prev_ratio = prev_ens_yes / prev_ens_n

    if direction == "NO":
        # Stronger NO = fewer YES votes (lower ratio)
        if cur_ratio > prev_ratio:
            return None
    else:
        # Stronger YES = more YES votes (higher ratio)
        if cur_ratio < prev_ratio:
            return None

    # 5. Kelly sizing
    balance = db.get_balance()
    model_prob = current_scan.get("model_prob", 0)
    market_price = current_scan.get("market_price", 0.5)
    days_to_res = current_scan.get("days_to_resolution", 0)

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
    )
    if size <= 0:
        return None

    # 6. Exposure cap
    open_trades = db.get_open_trades()
    total_exposure = sum(t["size_usdc"] for t in open_trades)
    if balance > 0 and (total_exposure + size) / balance > _MAX_EXPOSURE:
        return None

    new_leg_number = leg_count + 1

    return {
        "size_usdc":        size,
        "direction":        direction,
        "model_prob":       model_prob,
        "market_price":     market_price,
        "ens_yes":          cur_ens_yes,
        "ens_n":            cur_ens_n,
        "ens_pct":          cur_ratio,
        "leg_number":       new_leg_number,
        "parent_trade_id":  parent_id,
        "hours_to_close":   hours_to_close,
        "days_to_resolution": days_to_res,
    }


def _hours_since(iso_str: str) -> float | None:
    """Return hours elapsed since an ISO timestamp, or None if unparseable."""
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600
    except Exception:
        return None
