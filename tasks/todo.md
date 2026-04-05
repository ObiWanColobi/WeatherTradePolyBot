# Weather Bot — Backlog (as of 2026-04-03)

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

**Live testing checklist (first real market resolution):**
- [ ] `SELECT * FROM trader_forecasts LIMIT 5` — confirm correct `direction`, `was_correct`, `entry_price`
- [ ] Watch resolution pass logs for `[warn] freeze_trader_forecasts failed` or `[untraded-freeze] err=N`
- [ ] After first temperature pass with data: confirm `actual_temperature` + `temperature_delta` populated
- [ ] Check `no_coords` count in temp pass isn't unexpectedly high (city key mismatch risk)

---

## Canary Feature (keep separate from Railbird the user)

Track a single designated sharp trader's rolling win rate as an early-warning signal that
market efficiency is compressing (if their edge shrinks, ours may follow).

- [ ] Add `canary_wallet` config key (null by default)
- [ ] `weather_canary.py` — tracks the canary's rolling 20-trade win rate, surfaces trend
- [ ] If canary win rate drops below 0.52 (rolling), log a warning in the bot dashboard
- [ ] Dashboard panel: canary rolling win rate chart (separate from main trader ensemble)
- [ ] Evaluate: should a deteriorating canary signal reduce our Kelly fraction?

---

## Pyramid / Scale-In Model

Size into winning positions rather than fixed-size single entries.

- [ ] Design: entry at initial Kelly size; add-on triggers when ensemble conviction increases
  (e.g. ens_pct crosses 0.80 with ≥20% price improvement since entry)
- [ ] Max pyramid depth: 2 add-ons per market (3 legs total)
- [ ] Each leg tracked as separate trade record in DB (or a `parent_trade_id` FK)
- [ ] Exit logic: close all legs together on ensemble flip; partial exits TBD
- [ ] Config keys: `pyramid_max_legs`, `pyramid_add_on_min_conviction`, `pyramid_add_on_min_price_improvement`

---

## Risk Management Gaps

- [ ] Max drawdown halt — auto-stop bot if total unrealized + realized loss exceeds 8% of starting balance
- [ ] Daily loss limit — halt entries if same-day closed P&L < -$X (configurable)
- [ ] Kill switch — check for `STOP` file at top of each poll loop; exit cleanly if present
- [ ] VaR(95%) display on dashboard — estimated max daily loss at current position sizes
- [ ] Brier Score tracking — proper calibration metric alongside win rate

---

## Model Accuracy Analysis (needs data)

Blocked until ~30+ resolved trades with `actual_temperature` populated.

- [ ] YES warm bias investigation — are YES bets underperforming because model is warm-biased, or because of entry timing at thin-market hours?
- [ ] Temperature delta histogram — distribution of |delta| at entry vs at resolution
- [ ] Edge tier calibration — does STRONG (≥30%) actually outperform EDGE (15-30%)?
- [ ] City-level accuracy report — which cities does the model forecast worst?

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

- [ ] Reverse-engineer Railbird's (user's wallet) apparent algorithm — what entry patterns show up in their trade history?
- [ ] Dashboard: add "last discovery run" timestamp + trader count to a sidebar stat
- [ ] `trader_discovery.py` — add scheduled auto-run (weekly?) so tracked_traders stays fresh without manual runs
- [ ] Investigate AMM wallets as fade signals — if they're consistently on the wrong side of sharp money, that's information
