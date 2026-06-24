# Forecast-Track Bake-Off Verdict (candidate #3, EMOS calibrated-forecast taker)

**Run date:** 2026-06-24 · **Params:** edge_threshold=5%, fire window=16h before close · **Source:** GEFS-31 (point-in-time, real inits)

| Candidate | Stage | ROI (low-fee / full-fee) | City majority? | CI low | Trades | Calib | Verdict |
|---|---|---|---|---|---|---|---|
| c3_forecast_taker | Calibration gate | n/a | n/a | n/a | 0 | FAIL (PIT) | DISQUALIFIED before P&L |

> Note: the auto-generated row reads "KILL (calibration gate failed)". The narrative below is the honest, nuanced verdict — it is **not** a flat kill. Read it.

## Universe (honest n)
- 20 cities, 517 gradeable city-days (METAR truth-covered). Held-out (latest-third by date): **171 forecast records**.
- Tune records: 346. This is the real, point-in-time, lookahead-free universe after the GEFS pull extended ensemble coverage through 2026-06-16.

## What happened — and why it's NOT a flat kill
The candidate was disqualified at the **calibration gate**, before any P&L was computed. But the breakdown is informative, not fatal:

- **EMOS beats the raw ensemble:** CRPS **2.498 < 2.806**. The dispersion correction genuinely improves the forecast over the raw histogram (`beats_raw = true`).
- **Dispersion is now essentially correct:** held-out PIT std = **0.291** vs the uniform target **0.289**. The under-dispersion that destroyed the prior strategy is **fixed**.
- **The gate failed on a small SYSTEMATIC BIAS, not miscalibrated spread:** PIT mean = **0.429** (target 0.5); held-out mean residual = **−1.43°F** (observations run ~1.4°F cooler than the EMOS mean); PIT deciles are left-loaded (15.2% / 12.3% in the lowest bins vs 4.7% in the top). KS p = 0.0028. MAE = 3.39°F.
- **Cause:** a single global EMOS intercept fit on the tune window (≈May 20–early June) over-predicts warm on the cooler held-out regime (≈June 8–15). This is a mean-bias / regime-shift issue, exactly the kind EMOS's mean term exists to handle.

## The gate worked as designed
It refused to let a still-miscalibrated forecast proceed to P&L — the precise check that would have caught the prior strategy before it lost capital. The earlier "raw-CRPS floor" concern is moot: 0 of 171 held-out records had near-zero spread, so the floor did no work; the gate is sound.

## Recommendation
**Iterate the forecast model before killing the forecast track.** The signal is close: dispersion is right and EMOS beats raw; only a ~1.4°F held-out warm bias blocks the gate. Worth trying, cheaply, in this order:
1. **Per-city EMOS intercepts** (or per-city bias recentering) — the global `a` masks city-level offsets.
2. **Rolling / shorter training window** that tracks the regime instead of one static tune split.
3. Re-run the gate; if PIT passes, the candidate finally reaches the P&L stage and we measure ROI at the ask (the actual go/no-go).

If, after honest bias correction, PIT still fails OR the held-out pessimistic-fee ROI is ≤ 0, the forecast track is a dead end and we move to (or kill toward) the weaker microstructure candidates.

**This is a "promising, not proven — iterate the forecast" finding, not a final kill.**
