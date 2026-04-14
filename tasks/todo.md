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

**Dashboard UI:**
- [ ] Custom HTML tables for open/closed positions — sortable columns that preserve parent/child leg grouping (current Streamlit dataframe sorting breaks inline sub-rows)

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

## WEAK-Tier Unanimous Ensemble Entry — COMPLETE

Allow WEAK-tier entries when meteorological ensemble is unanimous (0/69 or 69/69) and conviction is green. Enter at reduced Kelly with separate $50 cap. Rationale: model is maximally confident, the "WEAK" label just means thin price edge, not low confidence.

- [x] Design spec
- [x] Implementation — 7% edge floor, $50 hard cap (`kelly_max_bet_usdc_unanimous`), `[unanimous]` tag in skip logs and entry logs
- [x] Post-slippage net edge check — added 2026-04-10: real-world slippage on thin NO markets was 4-5%, eroding unanimous edge to <3% net. Added `entry_min_net_edge_pct: 0.05` gate (applies to all tiers) in decision layer step 5b. Also raised `extended_positions_min_net_edge` 0.03→0.05 for consistency. Tested and verified in live log review.

---

## Extended Positions (Scale-In) — COMPLETE

Spec: `docs/superpowers/specs/2026-04-08-extended-positions-design.md`
Plan: `docs/superpowers/plans/2026-04-08-extended-positions.md` (12 tasks)

Up to 2 add-on legs per position. Eligibility: time-band gating (12h spacing from close), 12h cooldown between legs, ensemble ratchet (conviction must hold or strengthen vs previous leg), fresh Kelly sizing, exposure cap. All legs exit together on any exit trigger. Dashboard shows grouped parent/child with per-leg expanders.

- [x] Config keys (4 new `extended_positions_*` params)
- [x] DB schema (`parent_trade_id`, `leg_number`) + 4 helper queries
- [x] Core eligibility logic (`weather_extended.py`)
- [x] Bot loop integration (`run_extended_positions_pass`)
- [x] Executor `place_extended_order` + `close_position`
- [x] Exit logic — all-legs-together close
- [x] Dashboard — open + closed position grouping with per-leg expanders
- [x] Integration verification (all files compile, imports clean)

**Revisit after 30+ post-fix resolved trades:** Re-evaluate whether add-on trigger should also incorporate (A) price improvement or (B) market confirmation. Only 7 post-fix trades as of 2026-04-08, zero resolved — starting with ensemble conviction only.

- [ ] Consider unrealized P&L gate on leg trades — skip add-ons if parent position is underwater (e.g. >-15% unr P&L). Rationale: even with strong ensemble conviction, negative P&L means market is moving against us. Counter-argument: contrarian model + confirmed ensemble = good averaging opportunity. Needs data to evaluate. (Added 2026-04-09 after Chicago >=56F double loss — though root cause was stale ensemble from 0.2h test cooldown, not missing P&L check)

Deployed to PythonAnywhere: pending — pre-push review completed 2026-04-10, logs/DB/screenshots clean, ready to push

---

## Risk Management — COMPLETE & DEPLOYED

Spec: `docs/superpowers/specs/2026-04-07-risk-management-design.md`
Plan: `docs/superpowers/plans/2026-04-07-risk-management.md` (10 tasks)

- [x] Daily loss circuit breaker — 15% of account value (realized losses only), halts entries
- [x] Circuit breaker override — dashboard button, suppresses for remainder of UTC day
- [x] Close all button — dashboard sidebar, double confirmation, re-entry block
- [x] Per-trade close button — per row in open positions table, double confirmation, re-entry block
- [x] Re-entry block list — JSON file IPC, prevents bot re-entering manually closed markets
- [x] Risk status banner — red HALTED banner on dashboard when circuit breaker trips
- [x] Email notifications — SMTP alerts for circuit breaker + manual close-all events
- [x] ~~Max drawdown halt~~ — intentionally excluded (not appropriate for prediction markets)
- [x] ~~Kill switch (STOP file)~~ — intentionally excluded (dashboard controls + process stop suffice)
- [x] ~~VaR(95%)~~ — intentionally excluded (binary outcomes, not useful)

Deployed to PythonAnywhere 2026-04-08.

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

---

## Live Trading Migration — Phases

### Phase 0 — Branch Strategy — COMPLETE (2026-04-12)

- [x] Create `live` branch from `main` (main stays paper trading)
- [x] Establish shared-core architecture: decision engine, forecast layer, risk logic, DB schema stay identical across branches
- [x] Divergent files only: `executor/live.py`, `config.py` (live defaults), deployment scripts
- [x] Define merge strategy: `main` → `live` cherry-picks for shared logic updates; `live` never merges back into `main`

### Phase 1 — Live Executor + Wallet Management — COMPLETE (2026-04-12)

Plan: `docs/superpowers/plans/2026-04-12-phase1-live-executor.md` (15 tasks)

- [x] Uncomment `py-clob-client` in `requirements.txt` and verify installation
- [x] Create `executor/live.py` implementing `BaseExecutor` interface
  - [x] Order signing (ECDSA via py-clob-client)
  - [x] Order submission (POST to CLOB `/order` — FOK orders)
  - [x] Order status polling (fill confirmation, partial fills, rejections)
  - [x] Order cancellation (stale/unfilled orders)
  - [x] Fill price recording + slippage tracking (actual vs expected)
  - [x] Fee accounting (deduct Polymarket fees from P&L)
- [x] Wallet integration
  - [x] Load private key from `.env`
  - [x] Sync USDC balance from exchange on startup (replace DB-only balance)
  - [x] Allowance/approval check (CLOB contract authorized to spend USDC)
- [x] Position reconciliation on restart — balance sync implemented; full position import deferred to Phase 2
- [x] Paper/live mode toggle — `TRADING_MODE=paper|live` in `.env` selects executor
- [x] Allowance setup script — `scripts/setup_allowances.py` (one-time Polygon approval)
- [x] Live deploy script — `deploy_live.sh` for PythonAnywhere
- [x] 10 unit tests (all mocked, no real API calls) — all passing
- [x] Promoted `place_extended_order`, `close_position`, `settle_resolved` to `BaseExecutor` interface
- [x] Added `order_id`, `fee_usdc` columns to trades table + `set_balance()` DB function
- [x] Live config overrides: $25 max bet, $10 unanimous cap, 5% daily loss limit, no auto-reset

8 commits on `live` branch, pushed to GitHub.

### Phase 2 — Market Resolution & Claiming Winnings — COMPLETE (2026-04-12)

Spec: `docs/superpowers/specs/2026-04-12-phase2-claim-redeem-design.md`
Plan: `docs/superpowers/plans/2026-04-12-phase2-claim-redeem.md` (10 tasks)

- [x] On-chain claim/redeem — CTF `redeemPositions()` via web3.py (`chain/claimer.py`)
- [x] Two-phase settlement — winning trades set `claim_pending`, balance deferred until on-chain confirmation
- [x] Auto-claim on resolution — claims pass runs each bot loop after resolve pass
- [x] Track claim state in DB — `claim_status`, `claim_tx_hash`, `claim_retries`, `claim_last_attempt`
- [x] Retry with configurable backoff — [5m, 30m, 2hr, 8hr, 24hr], `claim_failed` after max retries
- [x] Gas guard — MATIC balance check before claiming, defer if below threshold
- [x] Account value includes `claim_pending` positions
- [x] 16 unit tests (7 claimer + 9 claim flow) — all passing
- [ ] Full lifecycle integration test (Task 9 — deferred to next session)
- [ ] Push to GitHub

### Phase 3 — Risk Recalibration for Real Money

- [x] Tighten circuit breaker (15% paper → 5% live) — pre-configured in `live_risk_daily_loss_limit_pct`
- [x] Reduce initial bet sizes ($25 max, $10 unanimous) — pre-configured in `live_kelly_max_bet_usdc`
- [ ] Emergency kill switch — halt all trading + cancel all open orders instantly
- [ ] Slippage kill-switch — if actual fill deviates >X% from expected, halt and alert
- [x] Order expiry / time-in-force — startup reconciliation cancels all leftover orders; FOK orders don't persist on book (2026-04-13)
- [ ] Nonce management (prevent wallet bricking from nonce collisions)

### Phase 4 — Monitoring & Alerts for 24/7 Operation — COMPLETE (2026-04-12)

Spec: `docs/superpowers/specs/2026-04-12-phase4-monitoring-alerts-design.md`
Plan: `docs/superpowers/plans/2026-04-12-phase4-monitoring-alerts.md` (14 tasks)

- [x] Discord webhook notifications (dual channel: #bot-alerts + #bot-trades)
- [x] Health heartbeat + crash detection on restart
- [x] Dashboard bot-down banner + notification feed panel
- [x] Shared API circuit breaker with escalating cooldowns
- [x] CLOB rate limiting (300ms delay floor)
- [x] Open-Meteo circuit breaker migrated to shared module
- [x] Daily P&L digest (UTC day rollover)
- [x] Trade alerts (fills, exits, claims)
- [x] Risk event alerts (circuit breaker, close-all)
- [x] API failure alerts (breaker trips, non-retriable errors, gas low)
- [x] CLOB API calls wrapped with circuit breaker

### Phase 5 — Live Validation (Small Stakes)

- [ ] Deploy to PythonAnywhere with $100-250 wallet
- [ ] Validate order execution, fills, fee deductions end-to-end
- [ ] Confirm balance reconciliation after restart
- [ ] Confirm auto-claim works on first resolved market
- [ ] Run for 1-2 weeks, review all trades manually
- [ ] Gradually increase bet sizes once validated
- [x] **Stale position reconciliation** — `reconcile_positions()` now auto-closes stale DB positions, queries CLOB trade history for real exit P&L, and cancels leftover open orders on startup. (2026-04-13)

---

## Potential Improvements (Low Priority)

- [ ] **Relayer API for gasless claims** — Replace direct web3 `redeemPositions()` with Polymarket's relayer (`py-builder-relayer-client`). Eliminates MATIC dependency and gas guard logic. Current gas cost is <$0.04/day so savings are negligible; main benefit is removing MATIC as a failure mode. Requires Polymarket proxy wallet address + Builder API credentials or Relayer API Key. Docs: https://docs.polymarket.com/developers/builders/relayer-client

---

## Minor / Polish

- [ ] Dashboard: add "last discovery run" timestamp + trader count to a sidebar stat
- [x] `trader_discovery.py` — weekly auto-run wired into calibration scheduler (daemon thread, non-blocking lock, timestamp written only on success)
- [ ] Trader data review & cleanup — monthly review of `tracked_traders` to prune stale/low-quality wallets; consider a script that deactivates wallets inactive for >90 days or with n_resolved < threshold after sufficient data accumulates
- [x] Deploy discovery scheduling changes to cloud bot — deployed to PythonAnywhere
- [x] After first cloud discovery run: confirm `[discovery] Complete.` appears in logs and `tracked_traders` row count updates — verified
- [ ] Investigate AMM wallets as fade signals — if they're consistently on the wrong side of sharp money, that's information
