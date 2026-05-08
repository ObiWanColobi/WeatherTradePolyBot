"""Phase2-06 — Live price-flip detector.

Shadow-only signal capture for the Phase 3 E2-02 NO-flip continuation gate.
Piggybacks on the bot's existing 60s scanner tick: for each market observed
this tick, compares the current YES midpoint against the oldest cached
midpoint inside the lookback window. If the absolute delta crosses the
threshold and the market isn't in cooldown, emits a flip event dict.

Cadence: ~60s sampling — coarser than dedicated polling but cheap and
sufficient for Phase 2 capture. Sub-tick flips are missed by design.

Tunables (env vars, all optional):
    FLIP_DETECTOR_THRESHOLD_PCT   default 0.05   absolute Δ in YES midpoint
    FLIP_DETECTOR_WINDOW_MIN      default 60.0   lookback window minutes
    FLIP_DETECTOR_COOLDOWN_SEC    default 300.0  silence after a fire
"""

from __future__ import annotations

import os
import time


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class FlipDetector:
    def __init__(
        self,
        threshold_pct: float | None = None,
        window_min:    float | None = None,
        cooldown_sec:  float | None = None,
    ):
        self._threshold   = threshold_pct if threshold_pct is not None else _envf("FLIP_DETECTOR_THRESHOLD_PCT", 0.05)
        self._window_sec  = (window_min  if window_min  is not None else _envf("FLIP_DETECTOR_WINDOW_MIN", 60.0)) * 60.0
        self._cooldown_sec = cooldown_sec if cooldown_sec is not None else _envf("FLIP_DETECTOR_COOLDOWN_SEC", 300.0)
        self._history:  dict[str, list[tuple[float, float]]] = {}
        self._last_fire: dict[str, float] = {}

    def observe(
        self,
        market_id: str,
        midpoint:  float,
        now_ts:    float | None = None,
    ) -> dict | None:
        """Append (now, midpoint) to per-market history; return a flip event
        dict when threshold is crossed, else None. Direction is 'YES_flip'
        when YES midpoint rose by ≥ threshold, 'NO_flip' when it fell.
        """
        if not market_id or midpoint is None:
            return None
        now_ts = time.time() if now_ts is None else now_ts

        hist = self._history.setdefault(market_id, [])
        cutoff = now_ts - self._window_sec
        while hist and hist[0][0] < cutoff:
            hist.pop(0)

        # Cooldown — keep recording history so the next eligible reading has
        # full context, but suppress firing.
        if (now_ts - self._last_fire.get(market_id, 0.0)) < self._cooldown_sec:
            hist.append((now_ts, midpoint))
            return None

        event: dict | None = None
        if hist:
            ref_ts, ref_mid = hist[0]
            delta = midpoint - ref_mid
            if abs(delta) >= self._threshold:
                self._last_fire[market_id] = now_ts
                event = {
                    "direction":      "YES_flip" if delta > 0 else "NO_flip",
                    "price_before":   ref_mid,
                    "price_after":    midpoint,
                    "delta_pct":      delta,
                    "minutes_window": (now_ts - ref_ts) / 60.0,
                }
                hist.clear()  # reset so next firing references post-flip prices

        hist.append((now_ts, midpoint))
        return event


def is_enabled() -> bool:
    """Default-on; flipped to false via env to disable shadow capture."""
    return os.environ.get("LIVE_FLIP_DETECTOR_ENABLED", "true").lower() != "false"
