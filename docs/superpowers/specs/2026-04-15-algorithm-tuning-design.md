# Algorithm Tuning — Design Spec
**Date:** 2026-04-15
**Based on:** [Trade Review Analysis](../../papercloudbotreport/trade_review_2026-04-15.md)

---

## Change 1: Remove YES Trades

**Files:** `weather_decision.py`

In `evaluate()`, after direction is computed (line ~100), reject any candidate where `direction == "yes"`:

```python
if direction == "yes":
    rejected.append(DecisionResult(
        candidate=candidate, verdict="REJECTED",
        reason="YES trades disabled", direction=direction,
        checks={},
    ))
    continue
```

Also remove the `entry_min_fill_price_yes` config key usage from `weather_entry.py` (dead code after this change). Leave the config key in `config.py` for reference but it will never be read.

**Extended positions:** `weather_extended.py` — add same guard. If an open position is somehow YES (legacy), skip extend evaluation.

---

## Change 2: Late-Game Market Divergence Exit

**Files:** `executor/weather_exit.py`, `config.py`

### New config keys
```python
"exit_late_game_hours":              8.0,   # window: last 8h before close
"exit_late_game_market_floor":       0.40,  # exit if our token drops below this
"exit_late_game_ensemble_threshold": 0.70,  # ...AND ensemble still shows >= this conviction for us
```

### Logic in `check_weather_exit()`

Insert as a **new check between ensemble flip (check 1) and adverse price move (check 2)**:

```
if hours_left is not None and hours_left <= 8.0:
    market_conviction = current_price  (already direction-adjusted)
    ensemble_conviction = direction-adjusted conviction from current_ensemble_pct
    
    if market_conviction < 0.40 and ensemble_conviction > 0.70:
        → exit: "late-game market divergence — market at {current_price:.1%}, 
                 ensemble still {conviction:.0%} conviction (stale forecast)"
```

### Remove the 2h exit lock

Current behavior (line 56): returns `should_exit=False` when `hours_left <= 2.0`.

**Replace with:** Remove entirely. The late-game divergence check and existing adverse/flip checks now cover the final hours. No blanket lock.

**Note:** The `exit_no_exit_hours_to_close` config key becomes unused. Remove it from `config.py`.

---

## Change 3: Raise Ensemble Conviction Threshold

**Files:** `config.py`

```python
"entry_min_ensemble_conviction": 0.85,  # was 0.70
```

Single config change. `weather_entry.py` reads this at module load via `_MIN_ENSEMBLE_CONVICTION`. No code changes needed beyond the config value.

**Impact:** Blocks trades where ensemble conviction is between 70-85%. Based on paper data, this filters out the "strong" and "moderate" tiers that had 43% WR.

---

## Change 4: Kelly Margin Scaling

**Files:** `weather_sizing.py`, `weather_decision.py`

### Concept
Scale Kelly confidence by how far the ensemble mean is from the threshold. Larger margin = more confident = bigger bet. Small margin = coin-flip risk = smaller bet.

### Implementation

`kelly_size()` gets a new parameter `ensemble_margin_c` (float, optional):

```python
# Ensemble margin scaling — bet size proportional to forecast distance from threshold
if ensemble_margin_c is not None:
    margin_mult = min(abs(ensemble_margin_c) / 5.0, 1.0)  # 0°C → 0.0x, 5°C+ → 1.0x
    kelly *= margin_mult
```

This goes after the existing `horizon_mult` discount, before fractional Kelly.

**Caller change:** `weather_decision.py` passes `ensemble_margin_c` from `scan_data.get("ensemble_margin_c")` into `kelly_size()`. Same for `weather_extended.py`.

### Rationale from data
- |delta| < 1°C: 43% WR (coin flip) — margin_mult ~0.1x
- |delta| 1-3°C: 76% WR — margin_mult 0.2-0.6x
- |delta| 3-5°C: 76% WR — margin_mult 0.6-1.0x
- |delta| 5°C+: 1.0x (full Kelly)

---

## Change 5: City Confidence Adjustments

**Files:** `config.py`, `weather_entry.py`

### New config keys
```python
"entry_city_adjustment_enabled": True,
"entry_city_strict_cities": [
    "chicago", "dallas", "atlanta", "toronto",      # US inland — weak ensemble reliability
    "london", "wellington",                           # coastal — temp swings
],
"entry_city_strict_min_conviction": 0.95,  # require near-unanimous for these cities (vs 0.85 default)
"entry_city_strict_min_margin_c":   4.0,   # require wider margin for these cities (vs 3.0 default)
```

### Logic in `weather_entry.py`

After the ensemble conviction check (check 1) and before the margin check (check 1b), add:

```python
city = scan_data.get("city", "").lower()
strict_cities = set(WEATHER.get("entry_city_strict_cities", []))

if city in strict_cities:
    strict_conviction = WEATHER.get("entry_city_strict_min_conviction", 0.95)
    if conviction < strict_conviction:
        → reject: "city {city} requires conviction >= {strict_conviction}"
```

For the margin check (check 1b), override `_MIN_ENSEMBLE_MARGIN_C` with the city-specific value when the city is in the strict list.

### Research TODO (deferred)
- Analyze temperature swing rates by latitude band
- Investigate inland vs coastal ensemble accuracy across Open-Meteo models
- Build a data-driven city reliability score after accumulating 100+ resolved trades

---

## Files Modified Summary

| File | Changes |
|------|---------|
| `config.py` | Conviction 0.70→0.85, new late-game keys, city strict list, remove `exit_no_exit_hours_to_close` |
| `weather_decision.py` | Reject YES direction, pass `ensemble_margin_c` to Kelly |
| `weather_entry.py` | City-strict conviction + margin override |
| `weather_sizing.py` | New `ensemble_margin_c` param, margin scaling multiplier |
| `executor/weather_exit.py` | Late-game divergence check, remove 2h lock |
| `weather_extended.py` | Skip YES positions, pass `ensemble_margin_c` to Kelly |

---

## What This Does NOT Change

- Adverse exit logic (unchanged — works well for NO tokens at $0.60-0.94 fill range)
- Unanimous bypass on adverse exits (unchanged for >12h; late-game check handles final hours)
- Extended positions mechanics (unchanged beyond YES guard + margin param)
- Risk management, claims, notifications (untouched)
- Scanner, forecast layer, calibration (untouched)
