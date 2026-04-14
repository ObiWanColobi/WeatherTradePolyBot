"""
Weather Exit Manager
─────────────────────
Exit logic for same-day weather markets. Fundamentally different from
the general exit manager — default is to HOLD to resolution, not ladder out.

Exit triggers (checked in priority order):
  1. Ensemble flip  — new model run shifts consensus >25pts from entry signal
  2. Price adverse  — market price moves >15c against position (crowd knows something)
  3. Within 2h close — never exit in final 2 hours (ride it out)

What we deliberately do NOT do:
  - Bleed-off ladders (leaving money on the table before resolution)
  - Trailing stops (too short a time horizon for meaningful price recovery)
  - Edge exhaustion exits (market correcting toward your price = hold, not exit)
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from config import WEATHER


# ── Config ────────────────────────────────────────────────────────────────────
_ENSEMBLE_FLIP_THRESHOLD  = WEATHER.get("exit_ensemble_flip_threshold",   0.25)
_ADVERSE_PRICE_MOVE_PCT   = WEATHER.get("exit_adverse_price_move_pct",    0.30)
_ADVERSE_MIN_MOVE         = WEATHER.get("exit_adverse_min_move_cents",    0.10)
_ADVERSE_MIN_HOLD_MINUTES = WEATHER.get("exit_adverse_min_hold_minutes",  60)
_ADVERSE_SKIP_UNANIMOUS   = WEATHER.get("exit_adverse_skip_unanimous_pct", 0.90)
_NO_EXIT_HOURS            = WEATHER.get("exit_no_exit_hours_to_close",    2.0)


@dataclass
class WeatherExitSignal:
    should_exit: bool
    reason:      str
    urgent:      bool = False


def check_weather_exit(trade: dict, market_data: dict, current_ensemble_pct: float | None) -> WeatherExitSignal:
    """
    Evaluate whether to exit an open weather position.

    Args:
        trade:                 open trade row from DB
        market_data:           current market state (price, liquidity, end_date, token_id)
        current_ensemble_pct:  current P(YES) from ensemble (0.0-1.0), or None if unavailable

    Returns:
        WeatherExitSignal — should_exit=False means hold.
    """

    # ── Never exit within 2 hours of resolution ───────────────────────────────
    # Use the trade's stored end_date (validated at entry time) rather than
    # re-fetching from the API, which can return a different field or stale value.
    hours_left = _hours_to_close(trade.get("end_date") or market_data.get("end_date", ""))
    if hours_left is not None and hours_left <= _NO_EXIT_HOURS:
        return WeatherExitSignal(
            should_exit=False,
            reason=f"within {_NO_EXIT_HOURS}h of close — holding to resolution",
        )

    fill_price    = trade.get("fill_price", 0.5)

    # current_price is maintained by update_open_positions() via CLOB midpoint.
    # For YES trades it's the YES token price; for NO trades it's the NO token price.
    # Both are on the same scale as fill_price, so the adverse check is symmetric
    # with no direction-aware logic needed. We never use the Gamma API price here —
    # that was the source of persistent 0.0000 false readings.
    current_price = trade.get("current_price")

    # ── 1. Ensemble flip ──────────────────────────────────────────────────────
    # Use raw yes/n counts to reconstruct entry P(YES) — entry_ensemble_pct
    # in the DB may be direction-adjusted (conviction) rather than raw P(YES),
    # while current_ensemble_pct is always raw P(YES) from yes/n.
    if current_ensemble_pct is not None:
        entry_ens_yes = trade.get("entry_ensemble_yes")
        entry_ens_n   = trade.get("entry_ensemble_n")

        if entry_ens_yes is not None and entry_ens_n and entry_ens_n > 0:
            entry_pyes = entry_ens_yes / entry_ens_n   # raw P(YES), same scale as current

            flip = abs(current_ensemble_pct - entry_pyes)

            # A flip is meaningful only if it crosses the midpoint conviction zone
            was_high = entry_pyes >= 0.70 or entry_pyes <= 0.30
            if was_high and flip >= _ENSEMBLE_FLIP_THRESHOLD:
                return WeatherExitSignal(
                    should_exit=True,
                    reason=(
                        f"ensemble flipped {entry_pyes:.0%} -> "
                        f"{current_ensemble_pct:.0%} ({flip:.0%} shift)"
                    ),
                    urgent=True,
                )

    # ── 2. Adverse price move ─────────────────────────────────────────────────
    # Both YES and NO use the same formula: fill_price - current_price.
    # YES: token price falling after entry is adverse.
    # NO:  token price falling after entry is adverse (crowd shifting away from NO).
    # This is now symmetric because current_price is always the price of the
    # token we actually hold (YES token for YES trades, NO token for NO trades).
    #
    # Guards:
    #   a) Cooldown — no adverse exit within first N minutes (post-entry settling)
    #   b) Unanimous — skip adverse exit entirely when ensemble was near-unanimous
    #      at entry (>=90% conviction). Trust the model; only ensemble flip can exit.
    if current_price is not None and fill_price > 0 and current_price > 0.001:
        # (a) Cooldown: skip adverse check if position is too new
        opened_at_str = trade.get("opened_at", "")
        minutes_held  = _minutes_since(opened_at_str)
        if minutes_held is not None and minutes_held < _ADVERSE_MIN_HOLD_MINUTES:
            pass  # skip adverse check — too soon after entry
        else:
            # (b) Unanimous bypass: skip if entry ensemble was near-unanimous
            # Use raw counts for consistency (entry_ensemble_pct may be
            # direction-adjusted in older trades).
            _ey = trade.get("entry_ensemble_yes")
            _en = trade.get("entry_ensemble_n")
            if _ey is not None and _en and _en > 0:
                _entry_pyes = _ey / _en
            else:
                _entry_pyes = None
            is_unanimous  = (
                _entry_pyes is not None
                and (_entry_pyes >= _ADVERSE_SKIP_UNANIMOUS
                     or _entry_pyes <= (1.0 - _ADVERSE_SKIP_UNANIMOUS))
            )

            if is_unanimous:
                pass  # near-unanimous ensemble — trust the model, hold to resolution
            else:
                adverse_move      = fill_price - current_price
                adverse_threshold = max(fill_price * _ADVERSE_PRICE_MOVE_PCT, _ADVERSE_MIN_MOVE)
                if adverse_move >= adverse_threshold:
                    return WeatherExitSignal(
                        should_exit=True,
                        reason=(
                            f"adverse price move ${adverse_move:.3f} "
                            f"(fill={fill_price:.3f}, now={current_price:.3f}, "
                            f"threshold=${adverse_threshold:.3f})"
                        ),
                        urgent=True,
                    )

    return WeatherExitSignal(should_exit=False, reason="hold — no exit condition met")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hours_to_close(end_date_str: str) -> float | None:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        delta = end - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600
    except Exception:
        return None


def _minutes_since(iso_str: str) -> float | None:
    """Return minutes elapsed since an ISO timestamp, or None if unparseable."""
    try:
        opened = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - opened
        return delta.total_seconds() / 60
    except Exception:
        return None
