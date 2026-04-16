"""Tests for executor.weather_exit.check_weather_exit."""
from datetime import datetime, timedelta, timezone

from executor.weather_exit import check_weather_exit


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
