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

_ENABLED             = WEATHER.get("extended_positions_enabled", True)
_MAX_ADD_ONS         = WEATHER.get("extended_positions_max_add_ons", 2)
_LEG_SPACING_H       = WEATHER.get("extended_positions_leg_spacing_hours", 12.0)
_MIN_COOLDOWN_H      = WEATHER.get("extended_positions_min_cooldown_hours", 12.0)
_REJECT_COOLDOWN_H   = WEATHER.get("extended_positions_rejection_cooldown_hours", 2.0)
_MAX_SLIPPAGE_PCT    = WEATHER.get("entry_max_slippage_pct", 0.05)
_MIN_NET_EDGE        = WEATHER.get("extended_positions_min_net_edge", 0.03)
_MAX_EXPOSURE             = WEATHER.get("decision_max_exposure_pct", 0.90)
_UNANIMOUS_MIN_CONVICTION = WEATHER.get("entry_unanimous_min_conviction", 0.97)

# In-memory cooldown for rejected extend attempts (trade_id -> last attempt datetime)
_rejected_attempts: dict[int, datetime] = {}


def _reject(trade: dict, reason: str) -> None:
    """Log rejection reason and return None."""
    city = (trade.get("city") or "?").title()
    print(f"  [extended] {city:<14} — skip: {reason}")
    return None


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
        return _reject(trade, "disabled")

    if current_scan is None:
        return _reject(trade, "no scan data")

    parent_id = trade["id"]
    direction = trade["direction"].upper()

    # 0. YES positions disabled (2026-04-15) — skip any legacy open YES position
    if direction == "YES":
        return _reject(trade, "YES trades disabled (algorithm tuning 2026-04-15)")

    # 0. Rejected-attempt cooldown (in-memory, survives only current session)
    last_attempt = _rejected_attempts.get(parent_id)
    if last_attempt is not None:
        hours_since_attempt = (datetime.now(timezone.utc) - last_attempt).total_seconds() / 3600
        if hours_since_attempt < _REJECT_COOLDOWN_H:
            return _reject(trade, f"rejection cooldown ({hours_since_attempt:.1f}h < {_REJECT_COOLDOWN_H}h)")

    # 1. Leg cap
    leg_count = db.get_leg_count(parent_id)
    if leg_count >= _MAX_ADD_ONS + 1:
        return _reject(trade, f"leg cap ({leg_count} >= {_MAX_ADD_ONS + 1})")

    # 2. Time band
    hours_to_close = current_scan.get("hours_to_close")
    if hours_to_close is None:
        return _reject(trade, "no hours_to_close")

    # 2b. Near-resolution price guard — skip if current token is already priced
    # near-binary (>=95% or <=5%). The CLOB locks such markets for new orders.
    market_price = current_scan.get("market_price", 0.5)
    if direction == "NO":
        token_price = 1.0 - market_price   # NO token price
    else:
        token_price = market_price
    if token_price >= 0.95 or token_price <= 0.05:
        return _reject(trade, f"near-binary (token={token_price:.2f})")

    # 2c. Cost-adjusted edge gate — skip if net edge after slippage isn't worth it
    model_prob_raw = current_scan.get("model_prob") or 0
    if direction == "NO":
        gross_edge = (1.0 - model_prob_raw) - token_price
    else:
        gross_edge = model_prob_raw - token_price
    slippage_cost = _MAX_SLIPPAGE_PCT * token_price
    net_edge = gross_edge - slippage_cost
    if net_edge < _MIN_NET_EDGE:
        return _reject(trade, f"net edge too low ({net_edge:.3f} < {_MIN_NET_EDGE})")

    add_on_index = leg_count  # 0-indexed: leg_count=1 means this is add-on #1
    required_hours = (_MAX_ADD_ONS - add_on_index + 1) * _LEG_SPACING_H
    if hours_to_close >= required_hours:
        return _reject(trade, f"time band (close={hours_to_close:.1f}h >= need<{required_hours:.0f}h)")

    # 3. Cooldown
    latest_leg = db.get_latest_leg(parent_id)
    if latest_leg:
        hours_since = _hours_since(latest_leg.get("opened_at", ""))
        if hours_since is not None and hours_since < _MIN_COOLDOWN_H:
            return _reject(trade, f"cooldown ({hours_since:.2f}h < {_MIN_COOLDOWN_H}h)")

    # 4. Ensemble ratchet — conviction must be equal or stronger than previous leg
    cur_ens_yes = current_scan.get("ens_yes")
    cur_ens_n   = current_scan.get("ens_n")
    if cur_ens_yes is None or not cur_ens_n:
        return _reject(trade, f"missing current ensemble (yes={cur_ens_yes}, n={cur_ens_n})")

    prev_ens_yes = latest_leg.get("entry_ensemble_yes") if latest_leg else None
    prev_ens_n   = latest_leg.get("entry_ensemble_n") if latest_leg else None
    if prev_ens_yes is None or not prev_ens_n:
        return _reject(trade, f"missing prev ensemble (yes={prev_ens_yes}, n={prev_ens_n})")

    cur_ratio  = cur_ens_yes / cur_ens_n
    prev_ratio = prev_ens_yes / prev_ens_n

    if direction == "NO":
        # Stronger NO = fewer YES votes (lower ratio)
        if cur_ratio > prev_ratio:
            return _reject(trade, f"ratchet weaker ({int(cur_ens_yes)}/{cur_ens_n} > {int(prev_ens_yes)}/{prev_ens_n})")
    else:
        # Stronger YES = more YES votes (higher ratio)
        if cur_ratio < prev_ratio:
            return _reject(trade, f"ratchet weaker ({int(cur_ens_yes)}/{cur_ens_n} < {int(prev_ens_yes)}/{prev_ens_n})")

    # 5. Kelly sizing
    # Re-derive unanimity from current ensemble — a normal-entry parent can reach
    # unanimous conviction by leg time, and vice versa. Don't inherit from parent.
    balance      = db.get_balance()
    model_prob   = current_scan.get("model_prob") or 0
    market_price = current_scan.get("market_price") or 0.5
    days_to_res  = current_scan.get("days_to_resolution") or 0
    cur_conviction = max(cur_ratio, 1.0 - cur_ratio)
    is_unanimous   = cur_conviction >= _UNANIMOUS_MIN_CONVICTION

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
        unanimous=is_unanimous,
    )
    if size <= 0:
        return _reject(trade, f"kelly size zero (mdl={model_prob:.2f} mkt={market_price:.2f})")

    # 6. Exposure cap
    open_trades = db.get_open_trades()
    total_exposure = sum(t["size_usdc"] for t in open_trades)
    if balance > 0 and (total_exposure + size) / balance > _MAX_EXPOSURE:
        return _reject(trade, f"exposure cap ({(total_exposure + size) / balance:.0%} > {_MAX_EXPOSURE:.0%})")

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


def record_extend_rejection(trade_id: int) -> None:
    """Call this when an extend order is rejected (slippage/thin book) to suppress
    re-evaluation of the same trade for _REJECT_COOLDOWN_H hours."""
    _rejected_attempts[trade_id] = datetime.now(timezone.utc)


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
