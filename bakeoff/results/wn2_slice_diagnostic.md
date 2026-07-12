# WeatherNext 2 Slice Diagnostic (Stage 1) — VERDICT: KILL (structural)

**Cities:** denver, dallas, chicago  ·  **Window:** 2026-04-01 → 2026-07-03  ·  **Run:** 2026-07-12
**Decision proxy:** latest init ≤ 12:00 UTC on (resolution_date − 1). Ideal PIT: mean≈0.5, std≈0.289 (uniform). Lower CRPS/MAE = better.

## Raw head-to-head (shared n=279 city-days, scored vs hourly METAR truth)

| Model | Shared city-days | MAE (°F) | CRPS | PIT mean | PIT std |
|---|---|---|---|---|---|
| WeatherNext 2 | 279 | 4.08 | 3.138 | 0.891 | 0.186 |
| GEFS-31 | 279 | 3.23 | 2.513 | 0.611 | 0.343 |

WN2 loses on every metric. PIT mean 0.891 = a large systematic **cold bias** (obs sits high in the member spread → members forecast too cold).

## Why it's a KILL — and it's structural, not marginal

The raw loss understates the problem, but investigation of the cold bias revealed the *decisive* reason to stop:

- **WeatherNext 2 forecasts at 6-hourly cadence (4 steps/local day).** GEFS is 3-hourly (8 steps/day); METAR truth is hourly (+:53 issuance).
- Polymarket weather markets **resolve on the daily HIGH temperature** — a peak that occurs at a specific hour (typically mid-afternoon). A 6-hourly forecast **structurally cannot resolve that peak**; it samples the day too sparsely to see the true daily max.
- Measured artifact: METAR hourly-max minus METAR max sampled on WN2's own 6-hourly grid = **~2.0°F mean / 1.8°F median** (n=474). So roughly HALF of WN2's ~4°F apparent cold bias is a pure daily-max **sampling artifact**, and GEFS's edge is partly unearned (denser grid, not necessarily better physics).

**But the artifact is the point, not an excuse.** The 6-hourly resolution is a fixed property of the WeatherNext 2 BigQuery product. Bias-recentering (EMOS) could remove the mean offset, but it cannot give the forecast the temporal resolution to see a daily peak it never sampled. **WN2 as delivered is intrinsically unfit for daily-high weather betting** — the measurement scale (6h) is coarse relative to the quantity being traded (hourly max). This is a structural rejection, independent of the model's underlying skill.

## Decision

**KILL. Do not broaden the pull. Do not proceed to Stage 2 (P&L).** User's call (2026-07-12): "6hrs vs 1hr is pretty big on the measurement scale — that alone kills this."

This is a strong-form ship-rejection (`feedback_backtest_validates_rejection_not_acceptance`): a structural unfitness, not a thin-n shrug.

## What was built (retained, local-only)
- `research_db/47_load_weathernext2.py` — WN2 BigQuery slice loader (3 cities, daily-max °F, GEFS-compatible). Reusable if a finer-resolution WN2 variable/product ever appears.
- `research_db/48_weathernext2_diagnostic.py` — WN2 vs GEFS vs METAR skill/calibration diagnostic.
- `research_db/weathernext2.duckdb` — 1.14M rows loaded (retained; cheap to re-pull).
- BigQuery access remains subscribed & verified (see memory `reference-weathernext2-bigquery`); cost is a non-issue (free tier). If a future WeatherNext product ships hourly or sub-hourly temperature, this harness re-runs immediately.
