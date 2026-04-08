# Weather Bot — Backlog (as of 2026-04-07)

Items completed this session have been removed. This list covers remaining work from the
start-of-session roadmap plus ongoing gaps.

---

## Stage 2 — Trader Shadow Forecast DB

Build a dataset of implied forecasts (from trader positions) vs actuals vs our model forecasts,
enabling per-trader accuracy scoring by city / season / geography over time.

- [x] Schema: `trader_forecasts` table — `(wallet, market_id, city, res_date, direction, entry_price, resolution, was_correct, temperature_delta)`
- [x] On each `trader_monitor.update()` call, snapshot current positions + resolution status into `trader_forecasts` when markets close
- [x] Join with calibration data (`actual_temperature`, `temperature_delta`) for city/region breakdown
- [x] Report view in dashboard: per-trader accuracy by region, by season, by edge tier
- [ ] Weight ensemble consensus by each trader's accuracy score (currently all qualified traders are equal weight)
- [x] Brier Score tracking — implemented in `db.py` and displayed on dashboard Trader Accuracy section

### Stage 2 Follow-ups (deferred, from 2026-04-04 design)

- [ ] Dashboard: pivot explorer with group-by / filter dropdowns
- [ ] Dashboard: per-trader drilldown page with forecast history
- [ ] Ensemble weighting: opt-in `trader_consensus_weighted` config flag + weighted variant of `get_trader_consensus`
- [ ] Ensemble weighting: replace equal-weight consensus with accuracy-weighted once ≥50 resolved forecasts per top trader accumulate

**Hardening (defer until observed in live testing):**
- [ ] Stuck-market cleanup: expire `trader_positions` rows for markets >30 days past `end_date` with no resolution detected
- [ ] `min_last_active_days` filter if stale-wallet noise shows up in dashboard data
- [ ] Investigate `no_metadata` spike risk in untraded-freeze pass — `scanner_cache` only holds latest scan; older markets won't be found
- [ ] Verify `global _last_ensemble_ts` / `_last_429_ts` timestamps actually update after first real ensemble fetch (`om.get_last_ensemble_ts()` should be non-zero)

**Bug fixed 2026-04-06:** `settle_resolved` in `paper.py` was not writing `actual_resolution`, `forecast_correct`, or `resolution_price` for held-to-resolution trades, and never called `freeze_trader_forecasts`. Fixed at source — data now written at resolution time. 3 backfilled trades (#78 NYC, #80 Dallas, #84 Chicago).

**Bug fixed 2026-04-07:** Calibration inversion bug — `weather_calibration.py` didn't invert CLOB midpoint for NO-direction trades. Corrupted `forecast_correct` and `actual_resolution` for 20 trades + 2 trader_forecasts. Fixed in code + repaired via `repair_forecast_data.py --apply`. True forecast accuracy: 73.5% (was incorrectly reported as 32.1%).

**Also fixed 2026-04-07:** Resolution retry cap (10 max attempts for no_data trades), ensemble margin raised 2C->3C, YES min_fill_price raised $0.15->$0.25.

**Live testing checklist (first real market resolution):**
- [x] `SELECT * FROM trader_forecasts LIMIT 5` — **PASS** (2 rows for Miami Apr 7, corrected by repair script 2026-04-07)
- [x] Watch resolution pass logs for `[warn] freeze_trader_forecasts failed` or `[untraded-freeze] err=N` — no issues observed
- [x] After first temperature pass with data: confirm `actual_temperature` + `temperature_delta` populated — **PASS** (35/36 closed trades)
- [x] Check `no_coords` count in temp pass isn't unexpectedly high — **PASS** (all 12 traded city keys match `CITY_COORDS` exactly)
- [x] Post-repair: verify new resolutions produce correct `forecast_correct` for NO-direction trades — verified after cloud deployment

---

## Pyramid / Scale-In Model

Size into winning positions rather than fixed-size single entries.

- [ ] Design: entry at initial Kelly size; add-on triggers when ensemble conviction increases (and its closer to time to close)
  (e.g. ens_pct crosses 0.80 with ≥20% price improvement since entry)
- [ ] Max pyramid depth: 2 add-ons per market (3 legs total)
- [ ] Each leg tracked as separate trade record in DB (or a `parent_trade_id` FK)
- [ ] Exit logic: close all legs together on ensemble flip; partial exits TBD
- [ ] Config keys: `pyramid_max_legs`, `pyramid_add_on_min_conviction`, `pyramid_add_on_min_price_improvement`

---

## Risk Management — DESIGNED, IMPLEMENTATION PENDING

Spec: `docs/superpowers/specs/2026-04-07-risk-management-design.md`
Plan: `docs/superpowers/plans/2026-04-07-risk-management.md` (10 tasks)

- [ ] Daily loss circuit breaker — 15% of account value (realized losses only), halts entries
- [ ] Circuit breaker override — dashboard button, suppresses for remainder of UTC day
- [ ] Close all button — dashboard sidebar, double confirmation, re-entry block
- [ ] Per-trade close button — per row in open positions table, double confirmation, re-entry block
- [ ] Re-entry block list — JSON file IPC, prevents bot re-entering manually closed markets
- [ ] Risk status banner — red HALTED banner on dashboard when circuit breaker trips
- [ ] Email notifications — SMTP alerts for circuit breaker + manual close-all events
- [x] ~~Max drawdown halt~~ — intentionally excluded (not appropriate for prediction markets)
- [x] ~~Kill switch (STOP file)~~ — intentionally excluded (dashboard controls + process stop suffice)
- [x] ~~VaR(95%)~~ — intentionally excluded (binary outcomes, not useful)

---

## Model Accuracy Analysis (unblocked — data is clean)

At 35 resolved trades with `actual_temperature` populated as of 2026-04-07. True forecast accuracy: 73.5%. Calibration data is now trustworthy after inversion bug fix.

- [x] YES warm bias investigation — **answered 2026-04-07:** YES underperformance is NOT warm bias. It's cheap-token adverse exits. 27% win rate on YES, but several correct forecasts (Seoul #76/#86, delta=+4.6C) killed by adverse exits on $0.09-$0.14 fills. Fixed by raising YES min_fill to $0.25.
- [ ] Temperature delta histogram — distribution of |delta| at entry vs at resolution
- [x] Edge tier calibration — implemented in `weather_calibration.py` (W/E/S tier breakdown in report)
- [x] City-level accuracy report — implemented in `weather_calibration.py` ("breakdown by City" in report)

---

## Self-Improvement Bot (exploratory)

Consider whether the decision layer can auto-tune its own thresholds based on resolved trade outcomes.

- [ ] Define: which config parameters are candidates for auto-tuning?
  (e.g. `entry_min_edge_pct`, `entry_min_ensemble_conviction`, `kelly_max_bet_usdc`)
- [ ] Evaluation window: last 50 resolved trades (rolling)
- [ ] Candidate approach: Bayesian optimization over threshold space, scoring by Brier + P&L
- [ ] Hard constraint: never auto-tune risk parameters (max_exposure, kill switch) — human approval required
- [ ] Feasibility: need ~100+ resolved trades for signal to be meaningful — park until data accumulates

---

## Minor / Polish

- [ ] Dashboard: add "last discovery run" timestamp + trader count to a sidebar stat
- [x] `trader_discovery.py` — weekly auto-run wired into calibration scheduler (daemon thread, non-blocking lock, timestamp written only on success)
- [ ] Trader data review & cleanup — monthly review of `tracked_traders` to prune stale/low-quality wallets; consider a script that deactivates wallets inactive for >90 days or with n_resolved < threshold after sufficient data accumulates
- [x] Deploy discovery scheduling changes to cloud bot — deployed to PythonAnywhere
- [x] After first cloud discovery run: confirm `[discovery] Complete.` appears in logs and `tracked_traders` row count updates — verified
- [ ] Investigate AMM wallets as fade signals — if they're consistently on the wrong side of sharp money, that's information
