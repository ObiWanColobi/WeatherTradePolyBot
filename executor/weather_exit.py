"""
Weather Exit Manager
─────────────────────
Exit logic for same-day weather markets. Fundamentally different from
the general exit manager — default is to HOLD to resolution, not ladder out.

Exit triggers (checked in priority order):
  1. Ensemble flip             — new model run shifts consensus >25pts from entry signal
  2. Late-game divergence      — within 8h of close, our token < 40% yet ensemble >= 70% (stale forecast)
  3. Price adverse             — market price moves significantly against position (crowd knows something)

What we deliberately do NOT do:
  - Bleed-off ladders (leaving money on the table before resolution)
  - Trailing stops (too short a time horizon for meaningful price recovery)
  - Edge exhaustion exits (market correcting toward your price = hold, not exit)
  - Blanket final-hour lock (removed 2026-04-15 — the late-game divergence check now covers this window)
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from config import WEATHER
from layers.layer3_weather import parse_threshold_c

if TYPE_CHECKING:
    from markets.metar_observer import MetarState


# ── Config ────────────────────────────────────────────────────────────────────
_ENSEMBLE_FLIP_THRESHOLD  = WEATHER.get("exit_ensemble_flip_threshold",   0.25)
_ADVERSE_PRICE_MOVE_PCT   = WEATHER.get("exit_adverse_price_move_pct",    0.30)
_ADVERSE_MIN_MOVE         = WEATHER.get("exit_adverse_min_move_cents",    0.10)
_ADVERSE_MIN_HOLD_MINUTES = WEATHER.get("exit_adverse_min_hold_minutes",  60)
_ADVERSE_SKIP_UNANIMOUS   = WEATHER.get("exit_adverse_skip_unanimous_pct", 0.90)
# Late-game market divergence (2026-04-15) — replaces blanket 2h exit lock
_LATE_GAME_HOURS          = WEATHER.get("exit_late_game_hours",              8.0)
_LATE_GAME_MARKET_FLOOR   = WEATHER.get("exit_late_game_market_floor",       0.40)
_LATE_GAME_ENS_THRESHOLD  = WEATHER.get("exit_late_game_ensemble_threshold", 0.70)


@dataclass
class WeatherExitSignal:
    should_exit: bool
    reason:      str
    urgent:      bool = False


def check_weather_exit(
    trade: dict,
    market_data: dict,
    current_ensemble_pct: float | None,
    *,
    metar_state: "MetarState | None" = None,
) -> WeatherExitSignal:
    """
    Evaluate whether to exit an open weather position.

    Args:
        trade:                 open trade row from DB
        market_data:           current market state (price, liquidity, end_date, token_id)
        current_ensemble_pct:  current P(YES) from ensemble (0.0-1.0), or None if unavailable
        metar_state:           current METAR state for the trade's city (keyword-only, optional)

    Returns:
        WeatherExitSignal — should_exit=False means hold.
    """
    # Hours remaining until market closes — used by the late-game divergence check
    # and read from the trade's stored end_date to avoid stale API re-fetches.
    hours_left    = _hours_to_close(trade.get("end_date") or market_data.get("end_date", ""))

    # ── Guard: market already closed → no live orderbook to sell into ────────
    # Once a market is past close the CLOB stops quoting, so every exit
    # attempt (late-game divergence, ensemble flip, adverse) ends in
    # "no midpoint" and loops forever. Hold and wait for resolution.
    # 30-min grace avoids over-suppression at the close boundary.
    if hours_left is not None and hours_left < -0.5:
        return WeatherExitSignal(
            should_exit=False,
            reason=f"awaiting resolution ({hours_left:.1f}h past close)",
            urgent=False,
        )

    fill_price    = trade.get("fill_price", 0.5)

    # current_price is maintained by update_open_positions() via CLOB midpoint.
    # For YES trades it's the YES token price; for NO trades it's the NO token price.
    # Both are on the same scale as fill_price, so the adverse check is symmetric
    # with no direction-aware logic needed. We never use the Gamma API price here —
    # that was the source of persistent 0.0000 false readings.
    current_price = trade.get("current_price")

    # ── 0. Observed resolution lock (METAR-based) ─────────────────────────────
    # Checked before all other triggers. Feature-flagged via metar_exit_on_lock.
    # Fails closed on any missing/uncertain input — never a false exit.
    if (
        WEATHER.get("metar_exit_on_lock", False)
        and metar_state is not None
        and not metar_state.is_stale
    ):
        parsed_thresh = parse_threshold_c(trade.get("threshold") or "")
        trade_dir     = (trade.get("direction") or "").lower()

        if parsed_thresh is not None and metar_state.max_today_c is not None:
            op, threshold_c = parsed_thresh

            # Lock-YES: observed max has already reached the "above" threshold.
            # Only meaningful for ">=" markets — a NO position on such a market is toast.
            if op == ">=" and trade_dir == "no":
                if metar_state.max_today_c >= threshold_c:
                    ts = (
                        metar_state.last_reading.observed_at_utc.strftime("%H:%MZ")
                        if metar_state.last_reading else "?"
                    )
                    return WeatherExitSignal(
                        should_exit=True,
                        reason=(
                            f"observed lock YES — {metar_state.icao} "
                            f"max={metar_state.max_today_c:.1f}°C "
                            f">= threshold {threshold_c:.1f}°C at {ts}"
                        ),
                        urgent=True,
                    )

            # Lock-NO: observed max already breached a "<=" threshold.
            # A YES position on such a market can no longer resolve YES.
            if op == "<=" and trade_dir == "yes":
                if metar_state.max_today_c > threshold_c:
                    ts = (
                        metar_state.last_reading.observed_at_utc.strftime("%H:%MZ")
                        if metar_state.last_reading else "?"
                    )
                    return WeatherExitSignal(
                        should_exit=True,
                        reason=(
                            f"observed lock NO — {metar_state.icao} "
                            f"max={metar_state.max_today_c:.1f}°C "
                            f"> threshold {threshold_c:.1f}°C at {ts}"
                        ),
                        urgent=True,
                    )

            # Conservative lock-NO for ">=" YES positions:
            # only exit if ensemble also shows P(YES) < 1% (plan §5.2).
            if op == ">=" and trade_dir == "yes":
                if (
                    metar_state.max_today_c < threshold_c
                    and current_ensemble_pct is not None
                    and current_ensemble_pct < 0.01
                ):
                    ts = (
                        metar_state.last_reading.observed_at_utc.strftime("%H:%MZ")
                        if metar_state.last_reading else "?"
                    )
                    return WeatherExitSignal(
                        should_exit=True,
                        reason=(
                            f"observed lock NO — {metar_state.icao} "
                            f"max={metar_state.max_today_c:.1f}°C "
                            f"< threshold {threshold_c:.1f}°C "
                            f"with ensemble P(YES)={current_ensemble_pct:.1%} at {ts}"
                        ),
                        urgent=True,
                    )

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

    # ── 2. Late-game market divergence (2026-04-15) ───────────────────────────
    # In the final hours, if our token price has collapsed but the ensemble
    # still shows strong conviction for us, the ensemble is stale and the
    # market is already pricing the true outcome. Exit before resolution.
    if (
        hours_left is not None
        and hours_left <= _LATE_GAME_HOURS
        and current_price is not None
        and current_ensemble_pct is not None
    ):
        trade_dir = (trade.get("direction") or "").lower()
        ens_conviction_for_us = (
            current_ensemble_pct if trade_dir == "yes" else (1.0 - current_ensemble_pct)
        )
        if (
            current_price < _LATE_GAME_MARKET_FLOOR
            and ens_conviction_for_us >= _LATE_GAME_ENS_THRESHOLD
        ):
            return WeatherExitSignal(
                should_exit=True,
                reason=(
                    f"late-game market divergence — {hours_left:.1f}h left, "
                    f"token at {current_price:.1%}, ensemble still "
                    f"{ens_conviction_for_us:.0%} conviction (stale forecast)"
                ),
                urgent=True,
            )

    # ── 3. Adverse price move ─────────────────────────────────────────────────
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
