# Cloud Bot Review — April 7, 2026

## Overall Performance

| Metric | Value |
|--------|-------|
| Total trades | 42 (36 closed, 6 open) |
| Closed P&L | **+$180.13** |
| Win rate | 55.6% (20/36) |
| Balance | ~$1,641 (started $1,000) |
| **Post-fix cohort (Apr 4+)** | **75% win rate, +$129.87 on 8 trades** |

The bot is profitable. Post-fix performance is strong — the April 4 changes (adverse floor, spread fix, margin filter) materially improved outcomes.

---

## BUG 1 (Critical): `forecast_correct` is inverted for NO-direction early-exit trades

**The reported 32% forecast accuracy is completely wrong. True accuracy is 73.5%.**

**Root cause:** `weather_calibration.py:207` calls `_clob_midpoint(trade["token_id"])` but for NO-direction trades, the `token_id` is the **NO token**, not the YES token. A NO token at 0.999 means NO won, but the code interprets it as YES won.

`weather_resolver.py:88-89` correctly does `yes_price = 1.0 - yes_price` for NO trades, but **calibration did not**.

**Impact:**
- 10+ trades had `forecast_correct` inverted in the DB
- `actual_resolution` was wrong for all calibration-backfilled NO trades
- `trader_forecasts.was_correct` was also corrupted (both Miami rows were inverted)
- Any calibration report or dashboard stat using these columns was unreliable

**Fix applied:** Added same inversion in `weather_calibration.py` after line 207. One-time repair script (`repair_forecast_data.py --apply`) corrected 20 trades + 2 trader_forecasts.

---

## BUG 2 (Medium): `freeze_trader_forecasts` blindly trusts `close_price`

`db.py:596` — `actual_resolution = "YES" if close_price > 0.5 else "NO"` assumes the caller already corrected for token direction. When called from `weather_calibration.py` (which didn't correct), `was_correct` for all tracked traders was inverted.

**Fix applied:** Docstring added clarifying `close_price` must be YES-equivalent. After Bug 1 fix, all callers now pass corrected prices.

---

## BUG 3 (Low): Persistent `no_data` count in resolution pass

The log showed `[calibration] Resolution: 1 resolved  1 settling  4 no_data` repeatedly throughout the day. Those 4 `no_data` trades would never resolve — their `token_id` had been delisted by Polymarket. The calibration pass kept attempting them every 15 polls (~15 min) forever, wasting CLOB API calls.

**Fix applied:** Added `resolution_attempts` column with cap of 10 retries. After 10 `no_data` results, trade is excluded from future passes.

---

## Operational Observations from Log

**Open-Meteo instability:** 15 point forecast failures, 678 rate-limit skips, 12 circuit breaker trips in ~10 hours. Toronto (43.65,-79.38) is particularly problematic — nearly every failed forecast targets those coords. The circuit breaker is working correctly (graceful degradation, stale cache served), but the frequency is notable.

**Poll rate:** 497 polls in ~10 hours = ~1 poll/72 seconds. Steady and consistent.

**Only 1 new entry** in 10 hours (Seoul NO >=15C, trade #105). The bot is correctly being selective, but almost every candidate is blocked by one of these gates:
- Spread too wide: 617 skips (most common)
- Ensemble conviction too low: 993 skips
- Ensemble margin too close: 1,015 skips
- Edge too small: 875 skips

**Temp pass 429 cooldown:** 24 occurrences where the temperature backfill was skipped due to Open-Meteo rate limiting. Not critical (eventual consistency), but shows the rate budget is tight.

---

## Trade Pattern Analysis

### What's working well

1. **Unanimous ensemble (0% or 100%) trades: 73% win rate, +$149.74.** These are the bot's bread and butter. When 0/69 or 69/69 ensemble members agree, the model is extremely reliable.

2. **NO-direction trades dominate:** 68% win rate, +$148.14. The bot excels at identifying conditions that WON'T be met. This makes sense — extreme threshold markets (>=30C, >=84F) are often mispriced high.

3. **Hold-to-resolution + spread exits at settlement:** 100% win rate on all trades that exited via `$1.000 > $0.220` or `resolved`. The exit logic is correct.

4. **Post-fix adverse exit:** Only 1 post-fix trade (#93 Seoul YES >=16C) triggered adverse exit, and that one was correctly stopped — the forecast was wrong (actual temp 15.6 < 16, delta=-0.4C, coin-flip zone). The cooldown + unanimous bypass is working.

### What's concerning

1. **YES-direction trades: 27% win rate, +$31.99.** Only 3 wins out of 11. Most of the losses are adverse exits on cheap YES tokens ($0.06-$0.19 fill). The min_fill_price gate at $0.15 helps but doesn't fully solve it. Note: several pre-fix YES trades were correct forecasts killed by adverse exits (#76 Seoul, #86 Seoul — both correct, delta=+4.6C).

2. **Coin-flip zone trades (|delta| < 2C): 14 trades.** These are inherently 50/50 bets but account for 39% of all trades with temperature data. P&L is +$78.45 only because of early exits before the coin landed wrong. The 2C margin filter caught some, but several slipped through (e.g., #64 Atlanta delta=0.2, #67 Buenos Aires delta=0.1).

3. **Trade #96 London NO >=24C:** Lost $11.06 on ensemble flip exit. Entered with ens=1.4% (nearly unanimous NO), but ensemble flipped to 32% two days later. This was a 54-hour-out entry on a market that was genuinely uncertain — the model updated correctly and the exit saved a bigger loss. Not a bug, but shows the risk of very early entries on distant markets.

---

## Changes Applied This Session

### Filter Tuning (applied 2026-04-07)

**Ensemble margin filter: 2C -> 3C**
- Reduces coin-flip zone trades where outcome is near-random
- Backtest: would have blocked 9 trades (6 wins, 3 losses, net +$63.75)
- Tradeoff: lose some marginal winners, but better EV per trade and lower variance

**YES min_fill_price: $0.15 -> $0.25**
- Blocks ultra-cheap YES tokens that consistently get killed by adverse exits
- Backtest: would have blocked 7 trades that were **all losses** (+$19.26 saved, 0 wins lost)
- Pure upside — no winning trades filtered out

**Combined impact (backtest):**

| Scenario | Trades | Win Rate | P&L |
|----------|--------|----------|-----|
| Original | 36 | 56% | +$180.13 |
| With both filters | 21 | **67%** | +$132.28 |

Win rate jumps 56% -> 67%. Total P&L lower due to fewer trades, but per-trade quality significantly improved — the right call as we approach live trading where consistency matters.

---

## Summary of All Changes

| Priority | Change | File(s) | Status |
|----------|--------|---------|--------|
| **CRITICAL** | Fix CLOB midpoint inversion for NO trades | `weather_calibration.py` | Done |
| **CRITICAL** | Fix `freeze_trader_forecasts` direction docs | `db.py` | Done |
| **HIGH** | Repair corrupted DB data (20 trades + 2 forecasts) | `repair_forecast_data.py` | Done (applied on cloud) |
| **MEDIUM** | Add `no_data` retry cap (10 max) | `weather_calibration.py`, `db.py` | Done |
| **LOW** | Raise margin filter 2C -> 3C | `config.py` | Done |
| **LOW** | Raise YES min_fill_price $0.15 -> $0.25 | `config.py`, `weather_entry.py` | Done |
