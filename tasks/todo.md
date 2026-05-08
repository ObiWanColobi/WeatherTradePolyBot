# Weather Bot — Backlog

---

## 📌 Pinned — Parked ideas (revisit triggers noted)

Strategic items that aren't ready to act on yet. Each has a clear gate before promotion to active work.

- [ ] **Tiered edge requirements by entry price** — gate on model-calibration audit first. Detail at [#upcoming-not-yet-started](#upcoming-not-yet-started) below.
- [ ] **Extended pass: skip past-close positions for add-ons** — small cleanup, low priority. Detail below and at [#extended-positions-scale-in--complete](#extended-positions-scale-in--complete).
- [ ] **Separate "awaiting resolution" from "open" positions** — design-first; touches decision layer, extended pass, exposure calc, dashboard. Detail at line ~177 below.
- [ ] **Trade volume concern** — user flagged 2026-05-02: ~5 trades/day vs 1000+ markets available. Revisit once METAR shadow + claim-wrap settle. Memory: [project_trade_volume_concern.md](../../C:/Users/Colby/.claude/projects/f--CodeProjects-TestCode1/memory/project_trade_volume_concern.md).
- [ ] **Unrealized-P&L gate on extended legs** — needs ~30+ resolved post-fix trades to evaluate. Detail under Extended Positions section below.
- [ ] **METAR Phase 1 — flip `LIVE_METAR_EXIT_ON_LOCK=true`** — held pending future-session review. State as of 2026-05-07: shadow code live since 2026-04-29 (commit `e8638a3` bootstrap fix); 1,636 paired obs in `metar_observations`, 21 cities, 81% ensemble-pairing rate, range 2026-04-29 → 2026-05-05. **Original time gate (~2026-05-06) is hit.** Two blockers remain before the flag can flip: (1) `scripts/metar_backtest.py` does not exist — the 2026-04-22 plan made the backtest the explicit go/no-go gate (replay `metar_observations` × closed trades, compute would-have-fired + P&L delta + false-positive count); (2) only 2 trades resolved during the shadow window (Toronto #47, Munich #48), so the live "zero false positives" check has effectively no statistical power without the backtest. **Decision deferred** to a future session — METAR is a defensive loss-recovery trigger, orthogonal to the E-series entry/sizing work, but with so many other research-stream changes queued (E15 Phase 0, G1_strict, E2-02, direction-agreement, E12-01) the user wants to review whether METAR is still worth prioritizing in that mix before writing the backtest. Plan: [plans/2026-04-22_METAR Data Implementation.md](plans/2026-04-22_METAR%20Data%20Implementation.md). Memory: [project_metar_resolution_frontrunning.md](../../C:/Users/Colby/.claude/projects/f--CodeProjects-TestCode1/memory/project_metar_resolution_frontrunning.md).

---

## 📌 Pinned — From E6 audit (2026-05-07)

Findings doc: [findings/2026-05-07_e6_audit_real_trades.md](findings/2026-05-07_e6_audit_real_trades.md). Bot-changes index: [findings/bot_changes.md](findings/bot_changes.md).

### High-confidence (act this cycle)

- [ ] **E6-13 — Hours-to-close window tightening (12-24h sweet spot)** — Live data: 12-24h bucket has n=8 / 100% win / +$32; 24-36h bucket has n=11 / 73% win / -$2 (bot's most-common entry zone, weakest yield). Median entry hours-to-close currently 20.3h. Action: investigate restricting `hours_to_close_max` toward ~30h, or weighting Kelly stake by hours-to-close band. Forward-shadow first.
- [ ] **Forward-shadow `trajectory_aware`** — Add a logged-but-non-acting check at decision time: `|D-1 ens-mean − D-5 ens-mean| ≥ 1°C AND direction-consistent with ensemble probability`. Capture for 30 days, compare per-trade Δ vs current. Backtest signal: +4.4% ROI at production / T06.
- [ ] **Drop or invert E2-01 fast-flip contra logic** — Backtest: 2.7% win rate when betting against fast price moves. Confirmed in audit as anti-edge. Remove from any candidate strategy; if revisited, test inverted direction (bet WITH the flip).
- [ ] **Skip `persistence_aware` deployment with the +20pp prior** — Backtest -$317 with the +20pp boost; user intuition agreed. Don't ship. If revisited, test smaller nudge (+5pp / +10pp) or per-city tuning.
- [x] **E6-15 — Data hygiene: city-name capitalization in trades (COMPLETE 2026-05-08)** — Backup at `weather_bot.db.bak_20260508_095839`. (a) Added `_norm_city(d)` helper in [db.py](../db.py) wired into `insert_trade`, `upsert_city_log`, `write_sizing_decision`, `record_metar_observation` (the `trader_forecasts` paths already use `city.lower()` inline). (b) One-shot `UPDATE … SET city = LOWER(TRIM(city))` ran across all 5 city-bearing tables: 5 rows normalized in `trades` (Munich/munich→munich(6), Tel Aviv/tel aviv→tel aviv(5), Denver/denver→denver(2), Dallas→dallas); 0 in the other 4 (already clean). Verified via integration test of all 4 dict-writers. **Tel-Aviv -100% ROI cohort investigation task is moot** — the 2 capitalized rows merged into the lowercase cohort, so the "separate winning vs losing tel-aviv cohort" was an artifact of the cap split.

### Forward-shadow candidates (lower confidence)

- [ ] **Forward-shadow the direction-agreement filter at PRODUCTION op-point** — The actual signal from `per_city_deterministic`. Best single backtest cell was at very-loose op-point (+$1,972 / 9.7% ROI / n=532) but very-loose uses **double-Kelly sizing** (margin 0.5) which is theoretically dangerous. The real finding is the **direction-agreement gate**: only enter when calibrated GEFS-31 ensemble AND city-best deterministic forecast (ICON for HK/TA/BA/Denver/Beijing/Istanbul; GFS for Shanghai/Paris/Chicago; per-city-MAE picks for the rest) point the same way. Shadow that gate at production op-point first (conviction 0.85, edge floor 1pp, half-Kelly). Plan: [plans/2026-05-07_direction_agreement_shadow.md](plans/2026-05-07_direction_agreement_shadow.md).
- [ ] **E6-14 — Local-time-of-day soft block at city-local 12-15h** — Live: n=3 trades, 0 wins, -$14.69. Anecdotal but consistent and low-cost to add as soft block in shadow mode (logged-but-non-acting).

### Investigation tasks

- [x] ~~**Tel-Aviv-capitalized -100% ROI cohort upstream cause**~~ — **MOOT 2026-05-08 via E6-15.** The cap-Tel-Aviv 2-trade losing cohort merged into the lowercase tel-aviv cohort (now 5 trades total). The "separate cohort" was a data-hygiene artifact, not a real upstream pattern.
- [ ] **Bot's actual entry-time distribution audit** — Backtest E6 found "decision-time T06 vs T12 swings $1,074." Live data shows median UTC hour 16, median local hour 8, median hours-to-close 20.3h — likely fine but worth confirming intentional.

### Next-session work (single-pass plan)

- [x] **Deliverable E7 — isolated testing + timing audit (COMPLETE 2026-05-07)** — Findings doc at [findings/2026-05-07_deliverable_E7_isolated_testing.md](findings/2026-05-07_deliverable_E7_isolated_testing.md). 17 helps / 3 neutral / 1 hurts / 3 framework-gap. Tclose-12h is the only positive timing cell for `current` (+$0.52/trade) — biggest lever surfaced. E4-03 rejected. E6 fast-flip lookahead leak found and fixed (E6 microstructure magnitudes were inflated; directional rejections still stand). bot_changes.md updated with new statuses.
- (Universe expansion + Direction-agreement now sequenced under "Research-stream sequencing" below — see §🥇 / §pinned-for-last)
- [x] **E8 — Tclose-12h selection-bias quantification (COMPLETE 2026-05-07)** — Findings doc at [findings/2026-05-07_deliverable_E8_tclose_selection_bias.md](findings/2026-05-07_deliverable_E8_tclose_selection_bias.md). Clock-shift IS real, not artifact. Paired BOTH cohort (n=474): Δ +$1.87/tr, 95% CI [+$0.55, +$3.39]. Mechanism: yes_price drift gives NO bet more shares per dollar at 12h. Trap warning: 12h-only cohort (n=59) is -$3.25/trade; clock-shift needs anti-momentum guardrail.
- [x] **E9 — Trap-guardrail design + backtest (COMPLETE 2026-05-07)** — Findings doc at [findings/2026-05-07_deliverable_E9_trap_guardrail.md](findings/2026-05-07_deliverable_E9_trap_guardrail.md). G1_strict ("would-have-fired-at-18h-equivalent") wins +$1,284 swing vs status quo. Counterintuitive: price-stability gates ALL lose money — they filter out the source of E8's resolved-NO lift. **E6-13 promoted to `code-review` with G1_strict spec.**

### Action-now follow-ups from E9

- [x] **E10 — 18h-anchor offset sensitivity sweep (COMPLETE 2026-05-07)** — Findings doc at [findings/2026-05-07_deliverable_E10_anchor_sensitivity.md](findings/2026-05-07_deliverable_E10_anchor_sensitivity.md). Sensitivity is flat: all four gaps {3h, 6h, 9h, 12h} land within $60 gross / $0.11 pnl-per-trade. Bot can use any cached yes_price from 3-12h prior. Spec stays at 6h.
- [x] **E11 — G1_strict composability check (COMPLETE 2026-05-07)** — Findings doc at [findings/2026-05-07_deliverable_E11_composability.md](findings/2026-05-07_deliverable_E11_composability.md). Composes cleanly with current/E2-02/E1-09; partial with B-01. **Strongest cell in entire E-series: E2-02 + G1_strict_6h = +$646 gross at +$2.20/tr on n=294.** Forward-shadow this as primary ship candidate.
### Research-stream sequencing (per `feedback_research_before_implementation.md`)

User-set ordering 2026-05-07 mid-session: complete ALL research before bot implementation. Bot phase begins only after the research stream closes; direction-agreement shadow code is pinned to AFTER bot implementation phase too.

#### 🥇 E12 — Universe expansion falsification (COMPLETE 2026-05-07)
- [x] **E12 — Universe expansion falsification (COMPLETE 2026-05-07)** — Cities: seoul, london, nyc, taipei, miami, ankara, atlanta, tokyo, seattle, wellington. Loaders 06/08/10/11/12/19 extended + run; ~45 min ingestion. Re-scoped mid-session from full E7/E11 re-run (8-12h) to slim falsification (option B, 2h actual). **Answer: NO structural reason to exclude any new city.** New-cohort EXCLUDE rate (50%) ≈ bot-cohort EXCLUDE rate (54%); per-city verdicts unreliable per Munich precedent. Forward-shadow candidates: London +$10.5/tr, Tokyo +$6.7/tr, Taipei +$2.2/tr, Seoul +$1.5/tr. Findings: [findings/2026-05-07_deliverable_E12_universe_falsification.md](findings/2026-05-07_deliverable_E12_universe_falsification.md). E12-01 added to bot_changes.md as `forward-shadow-gate`, pinned per research-first ordering AFTER bot implementation.

#### 🥈 Next: research follow-ups
- [ ] **E13 — E6 panel rebuild with Tclose-aware fast-flips** — From E7 follow-up #2. E6's `microstructure_gated` strategy and "E2-01 contra anti-edge" finding both used the panel-wide flip aggregation that E7 fixed. Magnitudes likely overstated; directional rejections probably hold. ~1-2h.
- [ ] **E14 — G1_strict × sizing-flavor strategies composability** — From E11 follow-up #3. Test G1_strict against E1-02, E1-05, E2-03, E3-07, E4-02. Likely composes (sizing is orthogonal to entry filter) but worth a confirmation run. ~30-45 min.
- [ ] **(conditional) Cluster cap calibration sweep** — From E7 follow-up #1. Only if E4-02 surfaces strongly post-shadow. Sweep cap ∈ {50, 75, 100, 150}. Skip if E4-02 doesn't surface.

#### 🥉 After all research: bot-side implementation phase
- [ ] **Bot-side G1_strict guardrail implementation** — Per E9/E10/E11 spec. Cache yes_price from prior poll per candidate market; re-evaluate would-have-fired-at-18h using cached price + unchanged ensemble. ~10-20 LOC, reversible config flag. Forward-shadow capture columns: `g1_anchor_yes_price`, `g1_anchor_would_fire`. **Do not start until research stream closes.**
- [ ] **Bot-side E2-02 NO-flip continuation gate** — Strongest backtest composite cell. Requires real-time flip detection plumbing (caveat from E11). Forward-shadow capture: `e2_02_flip_state` per trade. After G1_strict ships.

#### Pinned for last: Direction-agreement shadow code
- [ ] **Direction-agreement shadow code on bot side** — Plan: [plans/2026-05-07_direction_agreement_shadow.md](plans/2026-05-07_direction_agreement_shadow.md). 5 open questions (calibration map shippability, bot's calibrated-prob computation, per-city σ source, ICON cache freshness, city-name normalization). 5-6h bot work. **Pinned to AFTER all current research closes AND after G1_strict + E2-02 implementation ship.**

### Retracted from earlier E6 todo set

- ~~E6-04 explicit price-band gate~~ — already shipped via `_MIN_FILL_PRICE = 0.15` ([weather_entry.py:39](../weather_entry.py#L39)) + slippage / min-net-edge guards.
- ~~E6-11 per-city stake adjustment from backtest ranking~~ — REVERSED by live audit. Backtest per-city ranking does not match reality (Munich was called "drag" by backtest; reality says #1 profit center).

---

## Upcoming (not yet started)

- [ ] **Review tiered edge requirements by entry price** — Reasoning: at high entry prices (0.85+), the payoff ratio is asymmetric (risking $0.90 to win $0.10), so one loss wipes ~9 wins. Current edge thresholds are flat regardless of entry price. Evaluate whether to require progressively higher model edge at higher prices (e.g. 4% at 0.80, 7% at 0.90, 12% at 0.95) to self-select only highest-conviction entries at the most asymmetric price points. Gate on model calibration audit first — if win rate matches predicted probability, flat thresholds may be fine. Start with historical resolved trade analysis segmented by entry price bucket.

- [ ] **Extended pass: skip past-close positions for add-ons** (was first item, unchanged) —

- [ ] **Investigate end-of-session observation-dominated pricing edge** — Plan: [tasks/plans/2026-04-22_End-of-Session_Pricing_Edge.md](plans/2026-04-22_End-of-Session_Pricing_Edge.md). Trigger: rootdata article on Polymarket weather trading (https://www.rootdata.com/news/599077) describes "after ~3 PM local, observed temp beats morning ensemble" as one of six observed edges. Overlaps with METAR Phase 1.5b (intraday conditional max). This is a **backtest-first** gate: measure edge magnitude on our historical resolved trades before committing to build. GO/NO-GO decision feeds directly into METAR plan prioritization. Also captures smaller follow-on article insights — settlement-source change monitoring (Polymarket silently switched Shenzhen WU→NOAA 2026-03-29) and a possible "tail lottery" micro-sized carve-out around the 12h cooldown rule.

---

## 2026-04-22 — Wrong-date ensemble fallback + stale-read UI (COMPLETE — verified 2026-05-03)

All six tasks (A–F) are shipped in code on `live`:
- **A** — `find_forecast_day` / `find_ensemble_day` return `None` outside the window ([layers/layer3_weather.py:268-290](layers/layer3_weather.py#L268-L290)).
- **B** — `weather_bot.py` writes ensemble counts only when both values are non-None ([weather_bot.py:124-129](weather_bot.py#L124-L129) and [:166-171](weather_bot.py#L166-L171)).
- **C** — `current_ensemble_read_at` column added; written alongside counts on every valid read.
- **D** — Dashboard `_format_current_ens_cell` renders `(stale)` when `read_at` is missing or >300s old ([ui/weather_dashboard.py:102-134](ui/weather_dashboard.py#L102-L134)) plus caption at [:644-647](ui/weather_dashboard.py#L644-L647).
- **E** — One-time Shanghai cleanup was a no-op (handled by stale tag).
- **F** — `_reconcile_position_shares` shipped at [executor/live.py:1355](executor/live.py#L1355), wired into `initiate_exit` at [:1414](executor/live.py#L1414).

Original spec preserved below for history.

## 2026-04-22 — Wrong-date ensemble fallback + stale-read UI (original spec)

### Problem
`find_forecast_day` and `find_ensemble_day` at [layers/layer3_weather.py:267-284](layers/layer3_weather.py#L267-L284) silently fall back to `forecast[1]` / `ensemble[1]` (index 1) when the target date isn't in the cache. When a market's target date rolls out of the Open-Meteo window (happens late in the day in the market's local tz), the code reads **the next day's forecast** and stores it as `current_ensemble_yes/n` for the trade.

Concrete failure (Shanghai NO >=20C, April 22 market):
- At ~Apr 22 11:58 Shanghai local, cache had Apr 22 → ensemble=0/69 ≈ market=0.9% YES. Consistent.
- At ~Apr 23 00:30 Shanghai local, Apr 22 rolled out of cache. Cache now has Apr 23/24/25/26.
- `find_ensemble_day(cache, "2026-04-22")` falls back to `ensemble[1]` = **Apr 24** (mean 21.6°C, 68/69 ≥20°C votes).
- Trade's `current_ensemble_yes` is updated to 68 → `check_weather_exit` sees a 0→99% "flip" → fires URGENT exit at $0.99 on a position that will settle $1.00 in 7h.

### Blast radius
Every near-close market reads a wrong future date once the target rolls out of the forecast window. Affects `check_weather_exit` (ensemble flip, late-game divergence) and in principle `WeatherLayer.scan` (entry probability). Entry is self-healing because `scan()` already returns `None` if `point_temp_c is None` — but only after `find_forecast_day` returns `None`, which today it never does. Fix at the helper level cascades correctly to both sides.

### User requirement (added this round)
Don't just return `None` and let the dashboard show `—`. Keep the **last valid ensemble read** visible, with a stale indicator and timestamp of when it was last accurate. "I still want to be able to see the last real read of the ens (current)".

### Plan

- [ ] **A. Fix silent fallback** — [layers/layer3_weather.py:267-284](layers/layer3_weather.py#L267):
  - `find_forecast_day`: return `None` when `target_date` isn't in `forecast`. Delete the `forecast[1]` / `forecast[0]` fallback.
  - `find_ensemble_day`: return `None` when `target_date` isn't in `ensemble`. Delete the `ensemble[1]` fallback.
  - Update docstrings to say "returns None if target_date is outside the forecast window — callers must treat that as no signal".
  - Entry cascade: `scan()` at [layer3_weather.py:427](layers/layer3_weather.py#L427) already `return None` when `point_temp_c is None` — no change needed.
  - Exit cascade: `check_weather_exit()` at [executor/weather_exit.py:84](executor/weather_exit.py#L84) / [:113](executor/weather_exit.py#L113) already gate on `current_ensemble_pct is not None` — no change needed. It falls through to price-only exits (adverse move, late-game floor), which is the correct behavior.

- [ ] **B. Preserve last-good ensemble for UI** — don't overwrite with None:
  - [weather_bot.py:114](weather_bot.py#L114) already has `if ens_yes is not None and ens_n is not None:` gate before calling `db.update_trade`. That's correct — when `_get_current_ensemble` returns None, the DB row keeps its last good value. Verify this still holds after fix A (it will: `_get_current_ensemble` returns `(None, None, None)` when `scan` returns no `ensemble_n >= 10`, and `scan` will return None when `find_ensemble_day` returns None).
  - Same at [weather_bot.py:148](weather_bot.py#L148) for leg propagation — same gate, already correct.

- [ ] **C. Add read-at timestamp + DB migration** — needed for UI to show "last read at X":
  - Add `current_ensemble_read_at TEXT` via `_safe_add_column` at [db.py:201](db.py#L201) (next to existing `current_ensemble_n` line). Idempotent — safe to run repeatedly on VPS DB.
  - In [weather_bot.py:114-118](weather_bot.py#L114-L118), include `"current_ensemble_read_at": datetime.now(timezone.utc).isoformat()` in the same `db.update_trade` dict. Only written when the read is valid (same gate). Matches pattern at [weather_bot.py:151-154](weather_bot.py#L151) for legs.

- [ ] **D. Dashboard: stale indicator on Ens. Exit column** — [ui/weather_dashboard.py:834-835](ui/weather_dashboard.py#L834-L835) and :876-877 (leg rows):
  - Compute `is_stale = read_at is None or (now - read_at) > 3 * poll_interval_sec`. Use a hard 300s threshold — if ensemble hasn't been refreshed for 5 min on an open trade, something is off regardless of poll cadence.
  - When stale: render as `"68/69 (stale)"` in the Ens. Exit cell. Keep the numeric value — this is the explicit user ask.
  - Add a small caption / tooltip below the Open Positions table: "Ensemble marked (stale) means target date rolled out of the forecast window — last valid read shown". Anchor it so users know what the tag means.
  - No schema change to the row dict — just string formatting.

- [ ] **E. One-time cleanup for Shanghai row** — the DB currently has `current_ensemble_yes=68` from the wrong-date read. After fix A deploys, no new bad writes happen, but the stale Apr 24 value stays until the trade closes. That's actually OK — with the stale tag (fix D) it'll render as `68/69 (stale)`, which is truthful once `read_at` stops advancing. No migration needed beyond column add.

- [ ] **F. Keep share-reconciliation plan separate.** After fix A deploys, Shanghai's exit shouldn't have fired in the first place (`current_ensemble_pct=None` → no flip signal), so the circuit breaker will self-resolve on its next success. The dust-mismatch bug in `initiate_exit` is still real for legitimate future exits — fix it next, as a follow-up commit. Spec below.

### Verification after deploy

1. In `weather_bot` logs, Shanghai next poll should show `ens=0/69→?/?` (the `?` is because `_cur_ens` in the log at [weather_bot.py:135](weather_bot.py#L135) uses the returned ens values, which will be None). Then `-- hold — no exit condition met` (no flip because current_pct is None).
2. No more `[live] SELL order: ... shares=10.74` / `GTC sell failed` loop.
3. `[api_monitor] clob circuit breaker` goes quiet. After cooldown expires the breaker closes on the next successful CLOB call.
4. Dashboard Open Positions row for Shanghai shows `Ens. Exit: 68/69 (stale)` — last-good value preserved.
5. Unit test: add a case in `tests/test_weather_exit.py` confirming `current_ensemble_pct=None` does not fire `ensemble_flip` even with a near-unanimous entry ensemble.

### Commit plan
Single commit on `live`:
- `layers/layer3_weather.py` — helpers return None
- `db.py` — add `current_ensemble_read_at` column via `_safe_add_column`
- `weather_bot.py` — write `read_at` alongside ensemble counts
- `ui/weather_dashboard.py` — stale tag on Ens. Exit
- `tests/test_weather_exit.py` — None-handling test

Then push. User pulls on Kamatera.

---

## 2026-04-22 — Exit share reconciliation (COMPLETE — closed 2026-05-03)

`_reconcile_position_shares` is live at [executor/live.py:1355](executor/live.py#L1355) and wired into `initiate_exit` at [:1414](executor/live.py#L1414). The "Verify on Shanghai" task is moot — that position has long since closed; any future on-chain shortfall will be self-healed JIT at exit time. Original spec preserved below for history.

## 2026-04-22 — Exit share reconciliation (original spec)

### Problem
Shanghai NO exit stuck in a CLOB-reject loop tripping the API circuit breaker
(4 trips → 1800s cooldown). Root cause:

```
clob call failed: not enough balance / allowance:
  balance:      10,704,338  (= 10.704338 shares on-chain, scale 1e6)
  order amount: 10,740,000  (= 10.74 shares, from DB)
```

`initiate_exit()` at [executor/live.py:1109](executor/live.py#L1109) sums
`leg["shares"]` from the trades DB (set once at entry fill, db.py:552) and
posts a GTC sell for that amount. The on-chain ERC-1155 CTF balance is
~0.036 shares short of the recorded position — dust from a prior partial fill,
fee, or sibling-redeem remnant that never flowed back into the DB. Every poll
the sell retries the same over-sized amount; api_monitor escalates the
cooldown: 120s → 300s → 600s → 1800s.

### Root fix
Before posting a GTC sell, fetch the on-chain ERC-1155 balance via the existing
`self._claimer.get_token_balance(...)` helper (same call used at line 1610 in
the claim path), and reconcile DB `shares` down to match actual claimable
balance. Matches the pattern from commit cdc41af (sibling-redeem self-heal)
but at the exit-sizing layer instead of the claim layer.

### Plan

- [x] **Add `_reconcile_position_shares(legs, token_id) -> float`** in
      `executor/live.py`:
  - If `self._claimer is None` → return `sum(leg.shares)` (paper mode / no reconciliation possible).
  - Call `self._claimer.get_token_balance(proxy_address=WALLET_FUNDER_ADDRESS, token_id=int(token_id))`.
  - Convert raw uint256 → shares: `on_chain = balance_raw / 1e6`, then
    `math.floor(on_chain * 100) / 100` (matches the SELL-size truncation rule
    at line 164).
  - `recorded = sum(leg.shares)`.
  - If `on_chain >= recorded` → return `recorded` (no shortfall; normal case).
  - If `on_chain == 0` → log warning, return `0.0` (caller will skip exit;
    either RPC hiccup or already off-chain — don't mutate DB on zero).
  - If `0 < on_chain < recorded` → shortfall detected:
      - Reduce each leg's `shares` proportionally so `sum == on_chain`
        (`db.update_trade(leg["id"], {"shares": new_leg_shares})`).
      - Log `[live] RECONCILE {name}  DB={recorded:.4f} → chain={on_chain:.4f}`.
      - Discord `warning` notification (surprising event, worth visibility per
        `feedback_discord_notifications`).
      - Return `on_chain`.

- [x] **Wire into `initiate_exit()`** at line 1109:
  - Replace `total_shares = sum(leg.get("shares", 0) for leg in legs)` with
    `total_shares = self._reconcile_position_shares(legs, token_id)`.
  - Keep the existing `if total_shares <= 0: return` guard — now also catches
    the on-chain-zero case.

- [x] **No other changes to `_settle_exit`, `manage_pending_exit`, or partial
      exits.** After reconciliation the DB is self-consistent, so downstream
      math (leg_share_frac at line 1158, account value at db.py:310-317) keeps
      working without touching those paths.

- [ ] **Verify on Shanghai** — after deploy, the next poll should:
  1. Log `[live] RECONCILE Shanghai  DB=10.7400 → chain=10.7043`.
  2. Post a SELL at 10.70 shares (floor to 2dp).
  3. CLOB accepts, exit fills, api_monitor circuit breaker auto-resets on
     first success.

### Non-goals / deferred
- Not adding a broader "balance sweep" across all open positions each poll —
  too much RPC load. JIT-at-exit is sufficient and parallels the existing
  JIT-at-claim pattern.
- Not touching `record_account_value()` — reconciling downward at exit time is
  already a conservative signal (unrealized P&L was slightly over-reported,
  but account value only settles on close anyway).
- Not changing the circuit breaker thresholds. The fix removes the root cause;
  escalating cooldowns remain correct behavior for real CLOB failures.

### Commit plan
Single commit on `live` (no feature branch per
`feedback_no_feature_branches`). After commit, `git push` then user deploys
via `git pull` on Kamatera VPS (per `feedback_always_push_before_deploy`).

--- The extended pass evaluates positions with `hours_left < 0` for leg additions. Currently harmless (midpoint guard catches it: "Skipping add-on — no midpoint"), but wasteful and could succeed if CLOB briefly comes back. Add an `hours_left` guard early in the extended pass to skip past-close positions entirely.

- [ ] **Separate "awaiting resolution" from "open" positions** — Past-close positions waiting for Polymarket to resolve should not count as open. They hold position slots hostage, inflate unrealized P&L, and block exposure caps. Options: (A) new status like `awaiting_resolution` with a time-of-close transition, or (B) a separate DB query/view. Either way, decision layer, extended pass, exposure calc, and dashboard all need to stop treating them as active open positions. Design first — this touches many consumers.

---

## 2026-04-14 — Closed-position tracking + claim-error fix (CODE COMPLETE, pushed a7caf6a)

### Problem
Cloud bot (Kamatera, `weatherbot.service`) had 2 expired trades that vanished
from "Open Positions" and never appeared in "Trade History". Log showed:
1. `AttributeError: module 'db' has no attribute 'get_closed_trades'` at UTC
   midnight daily digest — crashed the bot.
2. Repeated `redeemPositions` reverts: "result for condition not received yet".
3. Duplicate DB rows (#9, #10) cloning #2 (HK) and #5 (Denver) after restart.

### Root causes
1. **Missing `db.get_closed_trades()`** — referenced by `_send_daily_digest`,
   never implemented.
2. **`reconcile_positions` ignored `claim_pending`** — only queried
   `get_open_trades()` when building `tracked_token_ids`. After the crash
   restart, on-chain HK/Denver positions matched no open DB row and were
   reimported as "orphaned" duplicates.
3. **Claim ran ahead of UMA oracle** — resolver uses CLOB midpoint (spot) to
   detect resolution, but `redeemPositions` needs `payoutDenominator > 0`. We
   fired claims before the oracle posted and burned retry slots on reverts.

### Phase A — Stop the crash + resume orphans correctly
- [x] Add `db.get_closed_trades()` → [db.py](../db.py)
- [x] `reconcile_positions` includes `get_pending_claims()` in
      `tracked_token_ids` → [executor/live.py](../executor/live.py)
- [x] Reconcile only calls `_close_stale_position` for `open_db_trades`
      (claim_pending trades are waiting on oracle, not stale)

### Phase C item 5 — Pre-claim oracle check
- [x] Add `payoutDenominator(bytes32)` to CTF ABI →
      [chain/abi/conditional_tokens.json](../chain/abi/conditional_tokens.json)
- [x] Add `Claimer.is_condition_resolved()` →
      [chain/claimer.py](../chain/claimer.py)
- [x] `process_pending_claims` calls `is_condition_resolved()` before
      `claim_winnings()`; `False` → defer silently, `None` (RPC error) → skip
      without burning a retry slot → [executor/live.py](../executor/live.py)

### Phase C item 6 Option A — Dashboard visibility
- [x] `weather_dashboard.py` trade history includes `claim_pending` rows →
      [ui/weather_dashboard.py](../ui/weather_dashboard.py)
- [x] Exit Reason column shows "(claim pending)" suffix until tx confirms
- [x] Existing claim-confirmation path at `executor/live.py` already flips row
      to `status='closed'` + credits balance when tx mines — no extra work

### Defense in depth
- [x] Partial unique index on `token_id` for `status IN ('open','claim_pending')`
      → [db.py](../db.py) `init_db()`. Creation wrapped in
      `try/except IntegrityError` so init doesn't crash on pre-existing dupes;
      logs a clear warning instead.
- [x] Startup duplicate-token detection (`_find_duplicate_active_tokens`) in
      `reconcile_positions`. If duplicates are found, reconcile aborts and
      fires a **critical** notification rather than silently continuing.

### DB cleanup
- [x] Receive fresh DB copy from user
- [x] `DELETE FROM trades WHERE id IN (9, 10);` (confirmed dupes of #2, #5)
- [x] `UPDATE trades SET claim_retries = 0, claim_last_attempt = NULL WHERE id IN (2, 5);`
- [x] Run `init_db()` — partial unique index created cleanly (no IntegrityError)
- [x] Sanity check: no duplicate parent `token_id` in active rows
- [x] Note: index excludes `parent_trade_id IS NOT NULL` (extended legs share token_id legitimately — rows #4/#8 Tel Aviv)

### Deploy
- [x] Deployed to Kamatera via git pull (PythonAnywhere discontinued 2026-04-14)
- [x] All fixes verified in live operation

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

Deployed to Kamatera (PythonAnywhere discontinued 2026-04-14).

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

Deployed to Kamatera (PythonAnywhere discontinued 2026-04-14).

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
- [x] Live deploy script — `deploy_live.sh` (now deploying to Kamatera via git pull)
- [x] 10 unit tests (all mocked, no real API calls) — all passing
- [x] Promoted `place_extended_order`, `close_position`, `settle_resolved` to `BaseExecutor` interface
- [x] Added `order_id`, `fee_usdc` columns to trades table + `set_balance()` DB function
- [x] Live config overrides: $25 max bet, $10 unanimous cap, 5% daily loss limit, no auto-reset

All pushed to GitHub and deployed to Kamatera.

### Phase 2 — Market Resolution & Claiming Winnings — COMPLETE (2026-04-12, deployed 2026-04-14)

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
- [x] Full lifecycle integration test — validated through live operation on Kamatera
- [x] Pushed to GitHub (live branch, deployed 2026-04-14)

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

### Phase 5 — Live Validation (Small Stakes) — COMPLETE (2026-04-14)

- [x] Deployed to Kamatera VPS with $200 wallet (PythonAnywhere discontinued)
- [x] Validated order execution, fills, fee deductions end-to-end
- [x] Confirmed balance reconciliation after restart
- [x] Confirmed auto-claim works on resolved markets (Dallas claim_pending verified)
- [x] Running live since 2026-04-14, 75% win rate on realized trades
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
- [x] Deploy discovery scheduling changes to cloud bot — deployed to Kamatera
- [x] After first cloud discovery run: confirm `[discovery] Complete.` appears in logs and `tracked_traders` row count updates — verified
- [ ] Investigate AMM wallets as fade signals — if they're consistently on the wrong side of sharp money, that's information
