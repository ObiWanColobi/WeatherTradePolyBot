"""
Weather Entry Conditions
─────────────────────────
Data-driven checks that gate whether the bot should enter a position
on a weather market. No time schedule — conditions speak for themselves.

All five must pass for an entry to be considered:
  1. Ensemble data      — hard block if <10 members (no sigmoid fallback for entries)
  2. Ensemble consensus — >70% or <30% (model conviction)
  3. Edge               — model vs market gap > min threshold
  4. Volume             — 24h volume above city-tier floor
  5. Spread             — bid/ask spread < max_spread_cents
  6. Hours to close     — at least min_hours_to_close remaining

Returns an EntryDecision dataclass with a boolean `ok` and a `reason`
string explaining which check failed (or "all checks passed").
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import WEATHER
from markets.polymarket import get_orderbook
import db
import trader_monitor

_session = requests.Session()
_session.headers.update({"User-Agent": "weather-bot/1.0"})

# ── Thresholds (from config with defaults) ────────────────────────────────────
_MIN_ENSEMBLE_CONVICTION = WEATHER.get("entry_min_ensemble_conviction", 0.70)
_MIN_EDGE_PCT            = WEATHER.get("entry_min_edge_pct",            0.10)
_MIN_VOLUME_DEFAULT      = WEATHER.get("scanner_min_volume",            5000)
_MIN_VOLUME_TIER1        = WEATHER.get("scanner_min_volume_tier1",      1000)
_TOP_CITIES              = set(WEATHER.get("top_cities",                []))
_MAX_SPREAD_CENTS        = WEATHER.get("entry_max_spread_cents",        0.10)
_MIN_HOURS_TO_CLOSE      = WEATHER.get("entry_min_hours_to_close",      2.0)
_MIN_FILL_PRICE          = WEATHER.get("entry_min_fill_price",          0.15)
_MIN_FILL_PRICE_YES      = WEATHER.get("entry_min_fill_price_yes",      0.25)
_MIN_ENSEMBLE_MARGIN_C   = WEATHER.get("entry_min_ensemble_margin_c",   2.0)


@dataclass
class EntryDecision:
    ok:     bool
    reason: str
    checks: dict   # individual check results for logging


def check_entry(market: dict, scan_data: dict, direction: str | None = None) -> EntryDecision:
    """
    Run all five entry checks against a market + its scan_data.

    Args:
        market:    dict from Polymarket API (price, volume, end_date, token_id, ...)
        scan_data: dict returned by WeatherLayer.scan() — must not be None

    Returns:
        EntryDecision with ok=True only if all checks pass.
    """
    checks = {}

    # ── 1. Ensemble conviction ────────────────────────────────────────────────
    ens_n   = scan_data.get("ensemble_n", 0)
    ens_yes = scan_data.get("yes_ensemble")

    # Hard block — ensemble is the entire basis of our edge. Sigmoid is only
    # useful for display/scanning; it must never gate real money.
    if ens_n < 10 or ens_yes is None:
        checks["ensemble"] = {
            "ok":    False,
            "value": f"no ensemble data (n={ens_n})",
            "need":  ">=10 ensemble members",
        }
        return EntryDecision(ok=False, reason="no ensemble data — entry blocked until models load", checks=checks)

    ens_pct    = ens_yes / ens_n
    conviction = max(ens_pct, 1 - ens_pct)
    checks["ensemble"] = {
        "ok":    conviction >= _MIN_ENSEMBLE_CONVICTION,
        "value": f"{ens_pct:.0%} {ens_yes}/{ens_n}",
        "need":  f">={_MIN_ENSEMBLE_CONVICTION:.0%} or <={1-_MIN_ENSEMBLE_CONVICTION:.0%}",
    }

    if not checks["ensemble"]["ok"]:
        return EntryDecision(ok=False, reason="ensemble conviction too low", checks=checks)

    # ── 1b. Ensemble margin (coin-flip filter) ────────────────────────────────
    # Even with 70%+ conviction, if the ensemble median is within 2°C of the
    # threshold the outcome is too close to call — small measurement differences
    # between Open-Meteo and Polymarket's oracle can flip the result.
    ens_margin = scan_data.get("ensemble_margin_c")
    if ens_margin is not None and _MIN_ENSEMBLE_MARGIN_C > 0:
        checks["ensemble_margin"] = {
            "ok":    abs(ens_margin) >= _MIN_ENSEMBLE_MARGIN_C,
            "value": f"{ens_margin:+.1f}°C",
            "need":  f">={_MIN_ENSEMBLE_MARGIN_C:.1f}°C from threshold",
        }
        if not checks["ensemble_margin"]["ok"]:
            return EntryDecision(
                ok=False,
                reason=f"ensemble margin {ens_margin:+.1f}°C too close to threshold (need >={_MIN_ENSEMBLE_MARGIN_C:.1f}°C)",
                checks=checks,
            )

    # ── 2. Edge ───────────────────────────────────────────────────────────────
    edge_pct = scan_data.get("edge_prob", 0)
    checks["edge"] = {
        "ok":    edge_pct >= _MIN_EDGE_PCT,
        "value": f"{edge_pct:.1%}",
        "need":  f">={_MIN_EDGE_PCT:.1%}",
    }
    if not checks["edge"]["ok"]:
        return EntryDecision(ok=False, reason="edge too small", checks=checks)

    # ── 3. Volume (two-tier) ──────────────────────────────────────────────────
    city      = scan_data.get("city", "")
    vol_floor = _MIN_VOLUME_TIER1 if city in _TOP_CITIES else _MIN_VOLUME_DEFAULT
    volume    = market.get("volume", 0)

    # Also check catalog avg if available (replaces hardcoded tier once data exists)
    catalog_avg = db.get_city_avg_volume(city, days=14)
    if catalog_avg is not None and catalog_avg > _MIN_VOLUME_DEFAULT:
        vol_floor = _MIN_VOLUME_TIER1   # city has proven high volume historically

    checks["volume"] = {
        "ok":    volume >= vol_floor,
        "value": f"${volume:,.0f}",
        "need":  f"${vol_floor:,.0f}",
    }
    if not checks["volume"]["ok"]:
        return EntryDecision(ok=False, reason="volume too low", checks=checks)

    # ── 4. Spread ─────────────────────────────────────────────────────────────
    spread = _get_spread(market.get("token_id"), market.get("price", 0.5))
    checks["spread"] = {
        "ok":    spread <= _MAX_SPREAD_CENTS,
        "value": f"${spread:.3f}",
        "need":  f"<=${_MAX_SPREAD_CENTS:.3f}",
    }
    if not checks["spread"]["ok"]:
        return EntryDecision(ok=False, reason="spread too wide", checks=checks)

    # ── 5. Hours to close ─────────────────────────────────────────────────────
    hours = _hours_to_close(market.get("end_date", ""))
    checks["hours_to_close"] = {
        "ok":    hours is not None and hours >= _MIN_HOURS_TO_CLOSE,
        "value": f"{hours:.1f}h" if hours is not None else "unknown",
        "need":  f">={_MIN_HOURS_TO_CLOSE}h",
    }
    if not checks["hours_to_close"]["ok"]:
        return EntryDecision(ok=False, reason="too close to resolution", checks=checks)

    # ── 6. Minimum fill price ─────────────────────────────────────────────────
    # Tokens priced below the floor are too cheap for stable position management:
    # normal spread noise triggers the adverse exit before the forecast can mature.
    # Applied per-direction using the YES market price.
    # Skipped when direction is unknown (backward-compatible with callers that
    # don't pass direction, e.g. dry-run tooling).
    if direction is not None:
        yes_price   = market.get("price", 0.5)
        fill_price  = yes_price if direction.lower() == "yes" else (1.0 - yes_price)
        floor       = _MIN_FILL_PRICE_YES if direction.lower() == "yes" else _MIN_FILL_PRICE
        checks["min_fill_price"] = {
            "ok":    fill_price >= floor,
            "value": f"${fill_price:.3f}",
            "need":  f">=${floor:.2f}",
        }
        if not checks["min_fill_price"]["ok"]:
            return EntryDecision(
                ok=False,
                reason=f"token price ${fill_price:.3f} below minimum ${floor:.2f} — noise risk too high",
                checks=checks,
            )

    # ── 7. Trader consensus (informational only — never blocks entry) ─────────
    if direction is not None:
        market_id = market.get("id", "")
        consensus = trader_monitor.get_consensus(market_id, direction)
        checks["trader_consensus"] = {
            "ok":     True,   # never blocks
            "signal": consensus["signal"],
            "same":   consensus["same"],
            "opp":    consensus["opposite"],
        }

    return EntryDecision(ok=True, reason="all checks passed", checks=checks)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_spread(token_id: str | None, mid: float) -> float:
    """
    Fetch the live order book and return the bid/ask spread in cents.
    Falls back to a wide spread (1.0) if the book is unavailable so the
    check fails safe rather than allowing a bad fill.
    """
    if not token_id:
        return 1.0
    try:
        book = get_orderbook(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return 1.0
        best_bid = float(max(bids, key=lambda x: float(x["price"]))["price"])
        best_ask = float(min(asks, key=lambda x: float(x["price"]))["price"])
        return best_ask - best_bid
    except Exception:
        return 1.0


def _hours_to_close(end_date_str: str) -> float | None:
    """Return hours until market closes, or None if unparseable."""
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        # If Polymarket returns a date-only string it parses as naive — assume UTC end of day
        if end.tzinfo is None:
            end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        delta = end - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600
    except Exception:
        return None
