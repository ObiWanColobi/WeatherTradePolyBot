# Weather Bot — Trade Review Analysis
**Date:** 2026-04-15
**Databases Reviewed:** `Paper_weather_bot.db` (Apr 2–14), `weather_bot.db` (Apr 13–15)

---

## Paper Bot Summary

| Metric | Value |
|--------|-------|
| Total Trades | 80 (75 parents, 5 add-on legs) |
| Closed / Open | 64 / 16 |
| Total P&L | **+$684.31** |
| Win Rate | **68.8%** (44W / 20L) |
| Total Deployed | $3,863.12 |
| ROI | 17.7% |
| Forecast Accuracy | 72.7% (40/55 resolved) |
| Date Range | Apr 2 – Apr 14, 2026 |

---

## Key Finding 1: NO Trades Are the Entire Business

| Direction | Trades | P&L | Win Rate | Avg Win | Avg Loss |
|-----------|--------|-----|----------|---------|----------|
| **NO** | 51 | **+$681.51** | **78.4%** | $21.67 | -$16.84 |
| YES | 13 | +$2.79 | 30.8% | $19.26 | -$8.25 |

YES trades contributed essentially nothing. Of 13 YES trades, 9 had fill prices below $0.20 — cheap tokens that got hit by adverse exits. The 3 profitable YES trades all had fill prices >$0.50.

**Decision: Remove YES trades entirely.**

---

## Key Finding 2: Only Unanimous Conviction Is Profitable

| Tier | Trades | P&L | Win Rate | Forecast Accuracy |
|------|--------|-----|----------|-------------------|
| **Unanimous (0-5%/95-100%)** | 45 | **+$634.14** | **82%** | 33/39 (85%) |
| Strong (5-15%/85-95%) | 7 | -$1.89 | 43% | 3/5 (60%) |
| Moderate (15-30%/70-85%) | 7 | +$53.79 | 43% | 3/7 (43%) |

Strong and moderate tiers are coin flips. 93% of all profit came from unanimous conviction trades.

**Decision: Raise conviction threshold from 0.70 to 0.85.**

---

## Key Finding 3: Entry Timing Matters Enormously

| Hours to Close | Trades | P&L | Win Rate |
|----------------|--------|-----|----------|
| **<6h** | 9 | **-$22.23** | **44%** |
| 6-12h | 2 | +$2.06 | 50% |
| **12-24h** | 8 | **+$135.78** | **88%** |
| **24-48h** | 27 | **+$394.77** | **70%** |
| 48h+ | 18 | +$173.93 | 72% |

Entries with <6h to close are negative EV. The 12-48h window is the sweet spot — 88% and 70% WR respectively.

**Decision: Raise minimum hours to close from 2h to 12h.**

---

## Key Finding 4: Phase-Over-Phase Improvement Was Dramatic

| Phase | Dates | Trades | P&L | Win Rate | Key Changes |
|-------|-------|--------|-----|----------|-------------|
| Initial | Apr 2-4 | 26 | +$27.48 | 46% | Default settings |
| First Tuning | Apr 4-7 | 13 | +$287.46 | 85% | Edge 12%, spread 8c, cache fix |
| Post-Cal Fix | Apr 7-10 | 10 | +$158.10 | 90% | Calibration inversion fixed, margin 3°C |
| Post-Exit Fix | Apr 10-14 | 15 | +$211.28 | 80% | Spread exit removed, net edge check added |

The Apr 2-4 cohort was the bot learning. After tuning, WR jumped to 85%+ and stabilized.

---

## Key Finding 5: $310 Left on Table from Premature Exits

Trades where the forecast was CORRECT but early exit (spread or adverse) took a loss:

| Trade | City | Actual P&L | Potential P&L | Left on Table | Exit Reason |
|-------|------|-----------|--------------|---------------|-------------|
| #86 | Seoul YES | -$4.04 | +$67.25 | $71.29 | adverse (fill=0.14→0.09) |
| #76 | Seoul YES | -$1.78 | +$52.03 | $53.81 | adverse (fill=0.09→0.06) |
| #118 | Munich NO | -$11.84 | +$41.50 | $53.35 | adverse (fill=0.49→0.34) |
| #99 | Madrid NO | +$40.76 | +$75.54 | $34.77 | spread exit (removed Apr 10) |
| #103 | Chicago NO | +$17.16 | +$45.97 | $28.81 | ensemble flip 0%→29% |

Removing spread exits (Apr 10) already fixed ~$55 of this. Most remaining losses were YES trades (now being removed).

---

## Key Finding 6: The Sweet Spot Profile

**Unanimous NO, fill >= $0.60, h2c 12-48h:** 34 trades, **+$421.92, 82% WR**

Top performers: Hong Kong ($258), Wellington ($80), Madrid ($74), Munich ($38).

---

## Key Finding 7: City Performance

### Winners (consistent)
| City | Trades | P&L | Win Rate | Forecast |
|------|--------|-----|----------|----------|
| Hong Kong | 6 | +$257.76 | 100% | 6/6 |
| Munich | 3 | +$92.45 | 67% | 3/3 |
| Wellington | 4 | +$80.46 | 75% | 4/4 |
| Madrid | 2 | +$74.40 | 100% | 2/2 |
| Paris | 3 | +$61.91 | 100% | 3/3 |
| London | 8 | +$63.18 | 62.5% | 4/7 |

### Problematic
| City | Trades | P&L | Win Rate | Notes |
|------|--------|-----|----------|-------|
| Toronto | 1 | -$92.44 | 0% | Single catastrophic loss |
| Dallas | 3 | -$47.31 | 33% | US inland city |
| Atlanta | 3 | -$9.28 | 0% | US inland city |
| Chicago | 7 | +$19.26 | 71% | 3 unanimous forecast failures |

US cities and coastal cities showed weaker ensemble reliability.

---

## Key Finding 8: Temperature Delta Predicts Outcomes

| Delta Range | Trades | P&L | Win Rate |
|-------------|--------|-----|----------|
| <1°C (coin flip) | 14 | +$25.04 | 43% |
| 1-3°C | 29 | +$302.15 | 76% |
| 3-5°C | 21 | +$357.12 | 76% |

When actual temperature was within 1°C of the threshold, it was a coin flip. The bot's edge comes from markets 1-5°C away from threshold.

---

## Key Finding 9: Unanimous Forecast Failures

6 trades where ensemble was unanimous (0-5% or 95-100%) but the forecast was wrong:

| Trade | City | Direction | Threshold | Ensemble | P&L | Delta |
|-------|------|-----------|-----------|----------|-----|-------|
| #127 | Dallas | YES | >=74F | 68/69 | -$50.00 | +0.2°C |
| #65 | Chicago | NO | <=57F | 1/69 | -$20.51 | -3.3°C |
| #96 | London | NO | >=24C | 1/69 | -$11.06 | +0.8°C |
| #117 | Chicago | NO | >=74F | 0/69 | -$2.07 | +2.9°C |
| #81 | Chicago | NO | <=57F | 3/69 | +$0.14 | -3.3°C |
| #83 | Chicago | NO | <=57F | 3/69 | +$13.89 | -3.5°C |

Notable: 3 of 6 failures were Chicago. Dallas and London also appear. Temperature deltas were small (0.2-3.3°C) — all near the coin-flip zone.

---

## Live Bot Summary (weather_bot.db)

| Metric | Value |
|--------|-------|
| Total Trades | 9 (7 parents, 2 add-on legs) |
| Closed | 3 |
| Open | 6 |
| Realized P&L | **+$6.15** |
| Unrealized P&L | **-$36.46** |
| Date Range | Apr 13 – Apr 15, 2026 |
| Balance | $142.23 |

### Closed Trades
| ID | City | Direction | Fill | Exit | P&L | Exit Reason |
|----|------|-----------|------|------|-----|-------------|
| 1 | Denver | NO | $0.91 | $0.89 | -$0.24 | Sold externally (reconciled) |
| 2 | Hong Kong | NO | $0.65 | $1.00 | +$5.29 | Resolved (claimed) |
| 5 | Denver | NO | $0.90 | $1.00 | +$1.10 | Resolved (claimed) |

### Critical: Two Ensemble Blowups

**Tel Aviv NO >=35C** (trades #4 + #8 add-on):
- Ensemble: 0/69 YES (unanimous NO — "no way it reaches 35°C")
- Current price: $0.001 (market resolved YES — it DID reach 35°C)
- Combined exposure: $19.91 → facing total loss
- **Root cause:** Ensemble was confidently wrong, market knew before ensemble updated

**Shanghai YES >=20C** (trades #7 + #11 add-on):
- Ensemble: 64-69/69 YES (near-unanimous — "it will reach 20°C")
- Current price: $0.001 (market resolved NO — it did NOT reach 20°C)
- Combined exposure: $20.37 → facing total loss
- **Root cause:** Same — stale ensemble, market moved to reality faster

**Key insight:** In the final 8-12 hours before resolution, the ensemble lags reality. Market participants react to observed temperatures while the ensemble is still running off forecasts made 12-48h earlier. The market becomes the better signal in the late game.

---

## Spread Exit Analysis (Removed Apr 10)

Of 34 spread-triggered exits in the paper data:
- **22 resolved anyway** (exit price was $0.998-$1.000 — spread exit was a no-op)
- **5 helped** (forecast was wrong, early exit saved some loss)
- **5 hurt** (forecast was correct, early exit cost us profit)

Removing spread exits was the right call. The hurt cases ($310 left on table) outweighed the help cases.

---

## Edge Score at Entry vs Outcome

| Edge Range | Trades | P&L | Win Rate |
|------------|--------|-----|----------|
| <10% | 2 | -$46.61 | 50% |
| 10-20% | 38 | +$253.44 | 74% |
| 20-30% | 8 | +$63.79 | 50% |
| 30-50% | 11 | +$272.67 | 73% |
| 50+% | 5 | +$141.02 | 60% |

The 10-20% edge bucket had the most trades (38) with a solid 74% WR. Higher edge scores correlate with larger per-trade P&L but not necessarily higher WR.

---

## Extended Positions Performance

4 closed add-on legs in paper data — all profitable:

| Parent | City | Leg P&L | Exit Reason |
|--------|------|---------|-------------|
| #111 | Hong Kong | +$79.31 | Spread exit (resolved anyway) |
| #114 | NYC | +$8.28 | Resolved |
| #119 | London | +$9.66 | Resolved |
| #122 | Tel Aviv | +$6.43 | Ensemble flip |

Extended positions worked well on unanimous NO trades. The 12h cooldown prevents stale-ensemble double-downs.

---

## Algorithm Changes — Agreed

| # | Change | Rationale |
|---|--------|-----------|
| 1 | **Remove YES trades entirely** | 30.8% WR, $2.79 total profit, cheap tokens get adverse-exited |
| 2 | **Late-game exit window (last 8h):** market floor at $0.40 + ensemble divergence check; remove 2h exit lock | Ensemble lags reality in final hours; market prices observed temps. Saves Tel Aviv/Shanghai scenarios |
| 3 | **Raise ensemble conviction** from 0.70 to 0.85 | Only unanimous tier is profitable; strong/moderate are coin flips |
| 4 | **Skipped** | YES removal eliminates cheap-token adverse exit problem |
| 5 | **Kelly margin scaling** — scale confidence by ensemble margin from threshold | Large margin = higher conviction = larger bet; small margin = coin-flip risk = smaller bet |
| 6 | **City confidence adjustments** — stricter requirements for US + coastal cities | US cities (Chicago, Dallas, Atlanta) and coastal (London) show weaker ensemble reliability. Research todo for latitude-based analysis |

---

## Day-Level P&L History

| Date | Trades | P&L | Deployed | Win Rate |
|------|--------|-----|----------|----------|
| Apr 2 | 16 | -$5.53 | $549.92 | 31% |
| Apr 3 | 10 | +$33.01 | $398.72 | 70% |
| Apr 4 | 4 | +$24.76 | $158.10 | 75% |
| Apr 5 | 4 | +$86.79 | $275.00 | 75% |
| Apr 6 | 5 | +$175.90 | $509.81 | 100% |
| Apr 7 | 3 | -$68.80 | $218.23 | 67% |
| Apr 8 | 6 | +$220.17 | $560.44 | 100% |
| Apr 9 | 1 | +$6.74 | $66.94 | 100% |
| Apr 10 | 9 | +$167.70 | $857.60 | 78% |
| Apr 11 | 3 | +$19.20 | $118.36 | 67% |
| Apr 12 | 1 | +$6.43 | $50.00 | 100% |
| Apr 13 | 2 | +$17.94 | $100.00 | 100% |
