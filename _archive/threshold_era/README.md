# Threshold-era modules (archived 2026-06-07)

Extracted from the threshold-era bot when it was replaced by the shotgun
paper trader (see docs/superpowers/specs/2026-06-07-shotgun-paper-trader-design.md).

These files are **reference source only** — NOT imported by the live bot. They
import config/db symbols that may no longer exist; they are not runnable in
isolation. Kept for future v2+ reuse:

- `weather_sizing.py`  — half-Kelly + horizon/margin scaling. Reuse: Kelly sizing mode for buckets.
- `weather_exit.py`    — adverse-move / ensemble-flip / late-game exits. Reuse: bucket exit rules (after paper-data backtest).
- `weather_extended.py`— scale-in / leg-spacing / cooldown. Reuse: re-fire-on-forecast-update.
