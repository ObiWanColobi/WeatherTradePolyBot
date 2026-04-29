# Live Trade Review — 2026-04-29

**DB snapshot:** weather_bot.db (modified 2026-04-29 11:37 PT)
**Review window:** 2026-04-21 → 2026-04-29 (one week since last review)
**Branch:** `live` @ 7389bc6
**Account value:** $4,034.35 (last balance_history at 2026-04-29 17:37 UTC)

---

## TL;DR

- **9W / 1L** in 10 closed trades since 04-21. **+$51.30** PnL on $550 deployed (**+9.3% ROI/week**).
- **Capital deposit landed 04-22**: account jumped $208 → $3,968 (final scaling tier, $4k tranche).
- Bet sizing ramped from ~$10 → ~$75 per leg as expected for the $4k tier.
- **2 open positions** (Shanghai NO, Buenos Aires NO), both deep ITM ($0.999 / $0.991), resolving today (04-29).
- **One loss: trade #29 Moscow `>=9C` NO**, −$9.99 — the same market type that motivated the METAR Phase 1 work.
- **🚨 Critical issue: METAR shadow DB is EMPTY.** 0 rows in `metar_observations` despite Phase 1 deploy on 04-23. Shadow collection appears to never have started.
- **Trader-forecast shadow DB also empty** (0 rows in `trader_forecasts`); `trader_positions` is collecting (latest snapshot today), so trader-monitor itself is alive.

---

## 1. Closed Trades, 2026-04-21 → 2026-04-29

| ID | City        | Threshold | Dir | Size   | Entry  | Peak  | PnL    | Reason   | Ens@open | Actual | Result |
|----|-------------|-----------|-----|--------|--------|-------|--------|----------|----------|--------|--------|
| 27 | istanbul    | >=20C     | NO  | $9.99  | 0.110  | 1.000 | +$1.11 | resolved | 0/69     | 20.5C  | ✅ leg2 |
| 28 | hong kong   | >=31C     | NO  | $10.04 | 0.077  | 1.000 | +$0.53 | resolved | 0/69     | 27.6C  | ✅      |
| 29 | **moscow**  | >=9C      | NO  | $9.99  | 0.145  | 0.970 | **−$9.99** | resolved | 0/69 | **8.6C** | ❌ |
| 32 | munich      | >=21C     | NO  | $74.98 | 0.125  | 1.000 | +$8.33 | resolved | 0/69     | 19.6C  | ✅      |
| 33 | tel aviv    | >=28C     | NO  | $74.98 | 0.070  | 1.000 | +$5.64 | resolved | 0/69     | 23.1C  | ✅      |
| 34 | chicago     | >=76F     | NO  | $74.98 | 0.090  | 1.000 | +$6.52 | resolved | 1/69     | 20.5C  | ✅      |
| 35 | munich      | >=22C     | NO  | $75.82 | 0.235  | 1.000 | +$22.65 | resolved | 0/69    | 21.0C  | ✅      |
| 36 | munich      | >=21C     | NO  | $73.31 | 0.095  | 1.000 | +$6.37 | resolved | 0/69     | 19.6C  | ✅ leg2 |
| 37 | istanbul    | >=22C     | NO  | $72.01 | 0.076  | 1.000 | +$5.42 | resolved | 0/69     | 21.8C  | ✅      |
| 38 | moscow      | <=7C      | NO  | $73.90 | 0.071  | 1.000 | +$4.72 | resolved | 0/69     | 10.3C  | ✅      |

**Aggregate:** 10 trades, $550.00 size, **+$51.30 PnL**, 9 wins. Win rate 90%. 0 fees recorded (live executor logs fees per trade — appears the new high-tier trades aren't writing `fee_usdc`; worth a follow-up).

**Algorithm signal quality:** Every winner went 0/69 → resolved at $1. Ensemble was correct on 9 of 10. No proactive exits fired (`exit_reason='resolved'` everywhere) — the algorithm is letting NO trades ride to settlement, which is the intended behavior since YES trades were disabled on 04-15.

---

## 2. The One Loss: Trade #29 Moscow `>=9C` NO

| Field | Value |
|------|-------|
| Opened | 2026-04-21 20:26 UTC (51.5h to close) |
| Closed | 2026-04-24 00:01 UTC (resolved YES) |
| Entry / Fill / Peak | 0.145 / 0.86 / 0.970 |
| Entry ensemble | **0 / 69 NO** (unanimous) |
| Final ensemble | 4 / 69 (5.8% YES) — never flipped |
| `actual_temperature` (DB) | **8.6 C** |
| `actual_resolution` (oracle) | **YES** (paid $1.00) |
| Loss | −$9.99 |

**Diagnosis:** classic ensemble blind spot. Bot recorded the daily reading as 8.6 C (NO should win), but Polymarket's resolution oracle (UUWW METAR) registered ≥9.0 C. Market price tracked the on-the-ground reading (peaked 0.97 YES) while our ensemble stayed at 4/69. None of the existing exit triggers fired:
- Ensemble flip threshold not met (5.8% << 50%)
- Late-game divergence requires market <40% AND high conviction — market was at 97%, opposite side
- Spread exit was removed earlier

**This is exactly the scenario [METAR Phase 1](../markets/metar_observer.py) was designed to catch.** Had the METAR exit been live, UUWW reading ≥9 C would have triggered a lock-YES exit before final resolution. Phase 1 shadow data was supposed to be feeding `metar_observations` for a backtest decision — see issue #1 below.

---

## 3. Open Positions (as of DB snapshot)

Both close 2026-04-29 and are deep ITM:

| ID | City         | Threshold | Dir | Size   | Entry | Current | End      | Status |
|----|--------------|-----------|-----|--------|-------|---------|----------|--------|
| 39 | shanghai     | <=12C     | NO  | $75.46 | 0.07  | 0.9995  | 04-29    | 80.28 shares — winner barring black swan |
| 40 | buenos aires | >=25C     | NO  | $75.22 | 0.071 | 0.991   | 04-29    | 80.02 shares — winner barring black swan |

If both resolve as expected, today adds approximately **+$13–14** to closed PnL.

---

## 4. Issues Found

### Issue #1 — 🚨 METAR shadow DB is empty (CRITICAL)

```
SELECT COUNT(*) FROM metar_observations;  -- 0
```

Phase 1 was deployed on **2026-04-23 (commit 3c23fc0)** with the rollout plan: set `METAR_ENABLED=true` on VPS, run for 1 week of shadow collection, then build/run the backtest before flipping `LIVE_METAR_EXIT_ON_LOCK=true`.

It's now been **6 days** and there are zero observations. The bot is running fine — `balance_history`, `trader_positions`, and `trades` all show fresh activity through 04-29 — so this is almost certainly that **`METAR_ENABLED=true` was never added to the VPS `.env`**.

**Action needed:**
1. SSH to VPS, check `.env` for `METAR_ENABLED`. If missing, add `METAR_ENABLED=true` and `systemctl restart weatherbot`.
2. Verify within ~1 hour that `metar_observations` rows are landing (~80–100/hour; ~480/day per memo).
3. Defer the 1-week shadow review until we actually have a week of data (target: ~05-06).
4. *Optional safety:* If the user wants to skip shadow and trust the implementation, we can flip `LIVE_METAR_EXIT_ON_LOCK=true` immediately, but the user explicitly wanted shadow data first.

### Issue #2 — Trader-forecast shadow (`trader_forecasts`) also empty

```
SELECT COUNT(*) FROM trader_forecasts;  -- 0
```

`trader_positions` *is* fresh (latest snapshot 2026-04-29 17:32 UTC, 1,459 tracked traders), so the discovery/monitor loop runs. But `trader_forecasts` — the entry-decision-time freeze table from Stage 2 — never gets written.

This may be intentional (Stage 2 was marked complete in memory, calibration bug fixed 04-07 with no outstanding items), or it may be a regression where the freeze hook was disconnected. Worth checking `trader_monitor.py` / `weather_decision.py` for the call site.

The user's prompt referenced a "shadow database not yet activated — pending data collection." If the intended shadow was `trader_forecasts`, then "we now have data" is not yet true.

### Issue #3 — `fee_usdc` column not populated

Every closed trade since 04-21 shows `fee_usdc = 0.0`. Polymarket charges maker/taker fees on real fills, so this should be a small but non-zero number. Either the executor stopped writing the column, or the value is being recorded under another name. Low priority but distorts net-PnL calculation if you ever want to see it.

### Issue #4 — `tracked_traders.last_active` format

Column stores Unix timestamps (e.g., `1776815754`) while the rest of the schema uses ISO datetime strings. Cosmetic but makes ad-hoc queries trip. Not introduced this week — flagged for future cleanup.

---

## 5. Memory Updates

After review, these memories need updating:

- **`project_live_trading.md`** — last updated 04-21 at $239.92, **stale**. Update to reflect $4,034 deposit on 04-22 and current state.
- **`project_metar_resolution_frontrunning.md`** — note that shadow collection has not started; `METAR_ENABLED` flag absent from VPS `.env`.
- **`project_scaling_ramp_plan.md`** — confirm tier-4 tranche has been deposited and bet sizes are now $75/leg.

---

## 6. Recommended Next Steps

1. **(blocker)** Fix METAR shadow: verify VPS `.env`, set `METAR_ENABLED=true`, restart weatherbot.
2. **(after #1)** Wait one week from real shadow start, then write `scripts/metar_backtest.py` and decide on `LIVE_METAR_EXIT_ON_LOCK`.
3. Investigate `fee_usdc=0.0` regression (Issue #3) — non-blocking but worth a 10-minute trace.
4. Decide whether `trader_forecasts` empty is intentional (Issue #2). If not, restore the freeze hook.
5. Update memory entries listed in §5.
