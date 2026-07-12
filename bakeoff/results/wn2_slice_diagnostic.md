# WeatherNext 2 Slice Diagnostic (Stage 1)

**Cities:** denver, dallas, chicago  ·  **Window:** 2026-04-01 → 2026-07-03
**Decision proxy:** latest init ≤ 12:00 UTC on (resolution_date − 1). Ideal PIT: mean≈0.5, std≈0.289 (uniform). Lower CRPS/MAE = better.

| Model | Shared city-days | MAE (°F) | CRPS | PIT mean | PIT std |
|---|---|---|---|---|---|
| WeatherNext 2 | 279 | 4.08 | 3.138 | 0.891 | 0.186 |
| GEFS-31 | 279 | 3.23 | 2.513 | 0.611 | 0.343 |

**Verdict:** NO INDICATION — WN2 does not beat GEFS on CRPS. Stop; do not broaden.
