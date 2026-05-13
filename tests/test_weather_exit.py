"""Tests for executor.weather_exit.check_weather_exit."""
from datetime import date, datetime, timedelta, timezone

import pytest

from executor.weather_exit import check_weather_exit
from markets.metar_observer import MetarReading, MetarState


def _iso(hours_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat()


def _trade(**overrides) -> dict:
    base = {
        "id":                 1,
        "market_name":        "Will the highest temperature in Hong Kong be 30C or higher?",
        "direction":          "NO",
        "fill_price":         0.65,
        "current_price":      0.001,
        "end_date":           _iso(12),
        "opened_at":          (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),
        "entry_ensemble_yes": 2,
        "entry_ensemble_n":   69,
    }
    base.update(overrides)
    return base


# ── Past-close guard ─────────────────────────────────────────────────────────
#
# Regression: closed markets must return should_exit=False so the executor
# never enters the "Cannot close — no midpoint" loop.

def test_holds_when_market_past_close():
    trade = _trade(
        end_date=_iso(-2),       # 2h past close
        current_price=0.001,
        entry_ensemble_yes=2,    # ensemble still says NO — would normally fire late-game
        entry_ensemble_n=69,
    )
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.03)

    assert sig.should_exit is False
    assert "awaiting resolution" in sig.reason
    assert "past close" in sig.reason
    assert sig.urgent is False


def test_holds_deeply_past_close_ignores_all_other_signals():
    """A trade 46h past close should hold even if every other exit condition fires."""
    trade = _trade(
        end_date=_iso(-46),
        current_price=0.001,        # adverse move
        entry_ensemble_yes=2,
        entry_ensemble_n=69,
    )
    # Simulate an ensemble flip that would also trigger exit
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.97)

    assert sig.should_exit is False
    assert "awaiting resolution" in sig.reason


# ── Grace boundary ───────────────────────────────────────────────────────────
#
# Guard: inside the 30-min grace window (0 to -30 min past close) late-game
# divergence still fires — we haven't given up on finding a sell fill yet.

def test_late_game_exit_still_fires_inside_grace_window():
    trade = _trade(
        end_date=_iso(-0.25),       # 15 min past close — inside grace
        direction="NO",
        fill_price=0.65,
        current_price=0.001,        # token collapsed
        entry_ensemble_yes=2,       # ensemble still 3% YES → 97% NO conviction
        entry_ensemble_n=69,
    )
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.03)

    # Late-game divergence conditions are met: ensemble still strong-NO and
    # token price has collapsed. With hours_left ~ -0.25 (inside the -0.5 grace),
    # the guard does NOT fire and downstream logic can still emit an exit.
    # We assert not the hold-guard reason — the exact reason depends on which
    # downstream check fires first, but it must not be the past-close hold.
    assert "awaiting resolution" not in sig.reason


# ── Active market regression ─────────────────────────────────────────────────

def test_active_market_late_game_divergence_still_fires():
    """Regression: the guard must not affect trades on still-open markets."""
    trade = _trade(
        end_date=_iso(2),           # 2h until close — active
        direction="NO",
        fill_price=0.65,
        current_price=0.001,
        entry_ensemble_yes=2,
        entry_ensemble_n=69,
    )
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.03)

    assert sig.should_exit is True
    assert "late-game market divergence" in sig.reason
    assert sig.urgent is True


# ── Unavailable ensemble ─────────────────────────────────────────────────────
#
# Regression: when the target date has rolled out of the Open-Meteo forecast
# window, find_ensemble_day returns None and _get_current_ensemble propagates
# None up. That must NOT trigger an ensemble-flip exit, even if the price
# signal alone might look adverse — the ensemble is simply unknowable right now.

def test_no_ensemble_flip_when_current_ensemble_is_none():
    """Entry ensemble was near-unanimous NO; current ensemble is unavailable.
    Must not fire ensemble-flip (which would need current_ensemble_pct to compare)."""
    trade = _trade(
        end_date=_iso(2),
        direction="NO",
        fill_price=0.94,
        current_price=0.9995,       # favorable — market says NO is winning
        entry_ensemble_yes=0,       # 0/69 YES at entry — unanimous NO
        entry_ensemble_n=69,
    )
    sig = check_weather_exit(trade, {}, current_ensemble_pct=None)

    # With favorable price and no current ensemble, nothing should fire.
    # Specifically: no "ensemble flipped" reason, which is the bug we just fixed.
    assert "ensemble flipped" not in sig.reason
    assert sig.should_exit is False


def test_no_late_game_divergence_when_current_ensemble_is_none():
    """Late-game divergence requires current_ensemble_pct — if None, skip it."""
    trade = _trade(
        end_date=_iso(0.5),         # 30 min until close — well inside late-game window
        direction="NO",
        fill_price=0.65,
        current_price=0.001,        # collapsed price would normally fire
        entry_ensemble_yes=2,
        entry_ensemble_n=69,
    )
    sig = check_weather_exit(trade, {}, current_ensemble_pct=None)

    assert "late-game market divergence" not in sig.reason


# ── Observed-lock date matching ──────────────────────────────────────────────
#
# Regression for the 2026-05-12/13 "weird exit reason" incident: the METAR lock
# was firing using yesterday's max for today's market. Two bugs combined:
#
#   A. metar_observer carried max_today_c forward across local-day rollover
#      → Tel Aviv #61 exited at 00:09 TLV May 13 with May 12's 34°C still cached.
#   B. weather_exit didn't verify the running max belonged to the market's day
#      → Hong Kong #56 exited at 16:05 HKT May 12 using May 12's 31°C peak
#        against the May 13 market (lost $28.99).
#
# Fix: MetarState now tracks max_today_local_date; weather_exit only fires the
# lock when that date equals the market's city-local resolution date.

def _metar_state(*, max_c: float, max_date: date | None, icao: str = "LLBG"):
    """Build a MetarState whose max_today_c is dated to `max_date`."""
    return MetarState(
        city="tel aviv",
        icao=icao,
        max_today_c=max_c,
        last_reading=MetarReading(
            icao=icao,
            observed_at_utc=datetime.now(timezone.utc) - timedelta(minutes=5),
            temp_c=max_c,
            dewpoint_c=None,
            raw_report="",
            source="avwx",
        ),
        last_fetched_at_utc=datetime.now(timezone.utc),
        is_stale=False,
        max_today_local_date=max_date,
    )


@pytest.fixture
def enable_lock(monkeypatch):
    """Flip metar_exit_on_lock on for the duration of the test."""
    from executor import weather_exit
    monkeypatch.setitem(weather_exit.WEATHER, "metar_exit_on_lock", True)


def test_observed_lock_fires_when_max_date_matches_market_date(enable_lock):
    """Positive: NO position, observed max breaches threshold, dates match → exit."""
    trade = _trade(
        end_date="2026-05-13",
        threshold=">=30C",
        direction="NO",
        fill_price=0.93,
        current_price=0.91,
    )
    state = _metar_state(max_c=34.0, max_date=date(2026, 5, 13))
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.0, metar_state=state)

    assert sig.should_exit is True
    assert "observed lock YES" in sig.reason
    assert sig.urgent is True


def test_observed_lock_blocked_when_max_is_from_yesterday(enable_lock):
    """Bug A regression — Tel Aviv #61.

    Market resolves May 13 TLV. max_today_c=34°C but it was set on May 12 (the
    accumulator before the fix carried the prior day forward at local midnight).
    With max_today_local_date=May 12 the lock must NOT fire on the May 13 market.
    """
    trade = _trade(
        end_date="2026-05-13",
        threshold=">=30C",
        direction="NO",
        fill_price=0.93,
        current_price=0.91,
    )
    state = _metar_state(max_c=34.0, max_date=date(2026, 5, 12))
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.0, metar_state=state)

    assert sig.should_exit is False
    assert "observed lock" not in sig.reason


def test_observed_lock_blocked_for_next_day_market(enable_lock):
    """Bug B regression — Hong Kong #56 / Toronto #63 / Munich #60 / Taipei #64.

    Bot evaluates a day-ahead market while still in the prior local day. Without
    the date guard, today's max (which is for a different day than the market)
    blindly locks the position. The lock must defer until the market's day starts.
    """
    trade = _trade(
        end_date="2026-05-14",    # day-ahead market
        threshold=">=14C",
        direction="NO",
        fill_price=0.81,
        current_price=0.85,
    )
    state = _metar_state(max_c=15.0, max_date=date(2026, 5, 13))  # today's max
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.0, metar_state=state)

    assert sig.should_exit is False
    assert "observed lock" not in sig.reason


def test_observed_lock_blocked_when_max_date_is_none(enable_lock):
    """Fail-closed: a state with no dated max (e.g. fresh boot) cannot lock."""
    trade = _trade(
        end_date="2026-05-13",
        threshold=">=30C",
        direction="NO",
        fill_price=0.93,
        current_price=0.91,         # avoid spurious adverse/late-game fire
    )
    state = _metar_state(max_c=34.0, max_date=None)
    sig = check_weather_exit(trade, {}, current_ensemble_pct=0.0, metar_state=state)

    assert sig.should_exit is False
    assert "observed lock" not in sig.reason
