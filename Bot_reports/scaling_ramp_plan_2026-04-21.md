# Weather Bot — Scaling Ramp Plan

**Date:** 2026-04-21
**Context:** Account currently funded at ~$240. Proposal under consideration: fund to **$4,000** and allow up to 10% of balance per trade (~$400/trade).
**Companion report:** `live_trade_review_2026-04-21.md`

---

## Summary recommendation

Fund the $4k. **Do not jump the config caps to match.** Scale the per-trade dollar caps in four tranches, gated on resolved-trade counts and sustained win rate. Rationale below the table.

---

## The ramp table

Columns are labeled by the **cumulative count of post-fix resolved trades** at which each tranche activates. "12" = today's state (11 won, 1 flip-exit since 2026-04-15). Promote by editing [config.py](../config.py) when the trigger count is reached *and* the health gates below hold.

| **Config key** ([config.py](../config.py)) | Current Live | **12 resolved** (now) | 32 resolved | **52 resolved** | 82 resolved |
|---|---|---|---|---|---|
| **`live_kelly_max_bet_usdc`** | $15 | **$50** | $100 | **$200** | $400 |
| **`live_kelly_max_bet_usdc_unanimous`** ⚠️ | $10 (inverted) | **$75** | $150 | **$300** | $600 |
| **`kelly_max_balance_pct`** | 10% | **2%** | 3.5% | **6%** | 10% |
| **`kelly_min_bet_usdc`** | $5 | **$5** | $5 | **$10** | $20 |
| **`kelly_fraction`** | 0.5 | **0.5** | 0.5 | **0.5** | 0.5–0.6 |
| **`decision_max_open_positions`** | 20 | **15** | 15 | **18** | 20 |
| **`decision_max_exposure_pct`** | 80% | **25% ($1k)** | 35% | **50%** | 60% |
| **`decision_max_positions_per_city_date`** | 2 | **1** | 2 | **2** | 2 |
| **`live_risk_daily_loss_limit_pct`** | 50% | **10% ($400)** | 15% | **20%** | 25% |
| **`entry_max_slippage_pct`** | 5% | **3%** | 3% | **4%** | 5% |
| **`entry_min_net_edge_pct`** | 5% | **6%** | 6% | **5%** | 5% |
| **`extended_positions_max_add_ons`** | 2 | **1** | 2 | **2** | 2 |
| **`extended_positions_min_net_edge`** | 5% | **6%** | 6% | **5%** | 5% |
| **`claim_min_matic_balance`** | 0.01 | **0.05** | 0.05 | **0.10** | 0.20 |
| **`api_breaker_trip_threshold`** | 3 | **3** | 3 | **4** | 5 |

All balance-% assumptions are based on $4,000 funded balance. Dollar caps bind first on most trades; percent caps are a secondary guard.

---

## Why not go straight to $400/trade

Three independent reasons, any of which alone would justify a slower ramp:

1. **Sample size is still tiny.** 12 resolved post-fix trades. 95% confidence interval on an 11/12 win rate is roughly 62–99%. The true edge could be anywhere in that range — we cannot distinguish "genuinely excellent" from "short lucky streak" yet. Scaling 27× on a sample of 12 is a bet that the observed WR is near the top of its confidence band.

2. **Fill mechanics change non-linearly with order size.** The bot's simulated slippage and `entry_max_slippage_pct: 0.05` gate were tuned for $10–15 orders that hit top-of-book. A $400 market buy eats 3–5 price levels on the typical Polymarket weather book — easily 2–5% of edge consumed per side. We have **zero live data** on actual fills at $50+, let alone $400. The tranches exist primarily to *collect that data* before the next bump.

3. **Claim pipeline has only ~6 days of production hours.** Neg-risk sibling-redeem self-heal (commit cdc41af) just confirmed working on 2026-04-22 02:53 UTC. A stuck $15 claim is an annoyance; a stuck $400 claim across multiple legs is a capital-allocation problem. We want more incident-free days before the dollar value of stuck claims matters.

---

## Per-row rationale

### Kelly caps

**`live_kelly_max_bet_usdc` ramp $15 → $50 → $100 → $200 → $400.** The ceiling on any single normal-conviction trade. $50 is ~3× current — enough to generate meaningful data on fill slippage at a new order scale without risking more than ~1.25% of a $4k balance on any one bet.

**`live_kelly_max_bet_usdc_unanimous` ramp $75 → $150 → $300 → $600.** Fixes the inversion at [config.py:165-166](../config.py#L165-L166). In paper config, unanimous cap is **$50 vs $15 default** (3.3× higher). In current live, it's **$10 vs $15 default** (0.67× — *lower*). Unanimous ensembles (0/69 or 69/69) are the bot's highest-signal entries — 82% WR in paper ([trade_review_2026-04-15.md Finding 6](trade_review_2026-04-15.md#L94)). They should get *larger* bets, not smaller. The 1.5× ratio in this plan is more conservative than paper's 3.3×, scaling up as confidence grows.

### Balance / exposure governors

**`kelly_max_balance_pct`: 2% → 10%.** At $4k, 2% is $80 — roughly binding alongside the $50/$75 dollar caps in Phase A. A belt-and-suspenders guard: if you ever raise a dollar cap manually, the balance % still protects. Only relaxes to the original 10% at Phase D when we have 82 resolved trades of live data.

**`decision_max_exposure_pct`: 80% → 25%.** Current 80% at $4k means up to $3,200 can be at risk across all open positions simultaneously — that is not really an exposure limit. 25% ($1,000) in Phase A still accommodates 15-20 concurrent positions at Phase A sizes. Relaxes to 60% at Phase D, which at $400/trade × 20 positions is still bounded at $2,400 at-risk ($1,600 reserve).

**`decision_max_positions_per_city_date`: 2 → 1 in Phase A.** Two positions on the same (city, date) with different thresholds means correlated exposure — same weather event, same forecast, same ensemble. Fine at $15/trade; concentrated at $50. Back to 2 in Phase B once we have more resolved-trade confirmation.

**`live_risk_daily_loss_limit_pct`: 50% → 10%.** The 04-16 legacy-cohort close booked −$40 across 4 positions. Same pattern at Phase A sizes = −$200 (5% of $4k); at Phase D = −$1,600 (40%). 10% daily limit ($400 at Phase A) gives headroom for 2 of those without tripping the circuit breaker but caps unknown-unknowns. Current 50% at $4k = $2,000/day loss allowed — not meaningfully a limit.

### Entry gates

**`entry_max_slippage_pct`: 5% → 3% → 3% → 4% → 5%.** Tighter early. The current 5% gate was set when real orders were ~$10-15 and simulated slippage rarely exceeded 2%. At larger order sizes, simulated slippage will climb — the 3% gate filters entries where the thin book would eat a bigger chunk of edge. Re-loosens once we have real fill data confirming execution matches simulation.

**`entry_min_net_edge_pct`: 5% → 6% → 6% → 5% → 5%.** Slight tightening during the ramp to offset the fact that real fill slippage may be higher than simulated slippage suggests. Net edge (edge after simulated slippage) staying ≥6% creates a safety buffer.

### Extended positions

**`extended_positions_max_add_ons`: 2 → 1 in Phase A.** An add-on leg on a position that's already moved in our favor compounds sizing risk. At $50 initial + $50 add-on + $50 add-on = $150 concentrated on one market. One leg max during ramp (initial entry + 1 add-on = 2 total); back to 2 legs (3 total) from Phase B.

**`extended_positions_min_net_edge`: 5% → 6%.** Same rationale as entry net edge — slightly tighter during ramp.

### Infrastructure

**`claim_min_matic_balance`: 0.01 → 0.05 → 0.10 → 0.20.** More trades → more claim transactions → more gas consumed. At Phase D the bot may claim 5-10 positions/day. 0.01 MATIC (~$0.005) is effectively empty and already triggered a "Gas Too Low" notification at 02:53 UTC today. A 0.20 floor gives days of buffer against MATIC price spikes or RPC issues — a small USDC equivalent in exchange for avoiding stuck claims.

**`api_breaker_trip_threshold`: 3 → 3 → 3 → 4 → 5.** Slightly more tolerant at scale because cancelling a $400 order due to a transient blip is more disruptive than cancelling a $15. Only relaxes once we've seen how the CLOB API behaves under Phase B+ order volume.

### Deliberately not touched

- **`entry_min_ensemble_conviction` (0.85)** — set by the 04-15 tuning based on paper data ([trade_review_2026-04-15.md Finding 2](trade_review_2026-04-15.md#L36)). Do not relax.
- **`entry_city_strict_*`** — data-driven list of weak cities (US inland + coastal). Do not relax.
- **`exit_late_game_*`** — the Tel Aviv / Shanghai safeguard. Do not relax; the dollar stakes of getting this wrong grow with size.
- **`kelly_fraction` (0.5)** — half-Kelly is the whole point. Only consider 0.5 → 0.6 at Phase D, and only after 82 resolved trades confirm the edge is stable.

---

## Promotion gates

Each phase must hold all of the following before promoting:

1. **Resolved-trade count** reached (see column headers).
2. **Rolling WR ≥ 70%** across all resolved trades in that phase.
3. **Zero orphaned positions** / reconcile surprises (`sold externally (reconciled)` events).
4. **Zero stuck claims > 48h** excluding documented UMA oracle delays (#22's 49h, #24's 24h are acceptable — those eventually cleared).
5. **Actual fill slippage ≤ phase `entry_max_slippage_pct`** — measured as `(fill_price − entry_mid) / entry_mid` on live trades. If real fills are materially worse than simulated, do not advance.

---

## Demotion gates

If any of the following happen mid-phase, **revert to the previous phase's config** and diagnose before re-promoting:

1. **10 consecutive resolved trades with WR < 60%** — strategy edge may be regime-dependent.
2. **Any circuit-breaker trip caused by real fill failures** (not orderbook-vanished scenarios already handled by bug #20).
3. **Any single-trade loss > 2× the phase's `live_kelly_max_bet_usdc`** — indicates a sizing or slippage bug.
4. **Any reappearance of reconcile-closure losses** — a pre-fix-style wipeout at Phase C/D scale is catastrophic.

---

## What this plan buys you

- **Phase A (now):** ~$50-75/trade, ~15-20 concurrent positions. Meaningful real-money data, ~$1k max at risk on any day. One bad week ≈ 10% account drawdown, not 40%.
- **Phase B (32 resolved):** First real "this is scaled up" tier. ~$100-150/trade, ~$1.4k max at risk. Typical weekly deployment ~$2-3k.
- **Phase C (52 resolved):** Majority of paper-bot backtest performance validated live. ~$200-300/trade.
- **Phase D (82 resolved):** Full target sizing. Requires ~6-10 weeks of clean trading at current volumes to reach.

At current volume (~2-3 trades/day, with ~12 resolved in 6 days), reaching 82 resolved trades takes roughly **6 weeks of clean operation**. That's the actual cost of the cautious ramp — not dollars, but calendar time.

If that timeline is too slow: the single most useful acceleration is **collapsing Phase B and C** (go direct to $200/trade at 32 resolved) rather than compressing Phase A. Phase A is specifically where we learn whether the strategy survives real-world fills at a new scale; we need those ~20 trades before doing anything larger.
