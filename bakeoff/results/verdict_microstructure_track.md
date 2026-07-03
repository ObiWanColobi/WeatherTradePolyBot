# Microstructure-Track Bake-Off Verdict (forecast-free candidates on real captured depth)

**Run date:** 2026-07-03 · **Universe:** 130 gradeable city-days / 20 cities (676 depth decision-rows), resolution dates **2026-06-26..2026-07-02** · **Truth:** METAR daily-max (IEM ASOS), forward-filled 06-15..07-03 into `research.duckdb` this session (`research_db/46b_metar_forward_fill.py`) · **Depth:** real CLOB `/book` ladders captured from 2026-06-25.

> **Supersedes the earlier "ungradeable" draft of this file.** That draft correctly diagnosed the truth gap (METAR ended 06-14, depth started 06-26) and prescribed ingesting METAR for the depth window. That has now been done — the candidates ran against real settlement. These are the graded results.

## ★ VERDICT: NO TRADEABLE EDGE — plug-pull territory

Every forecast-free candidate either **dies at the pessimistic fee** or is a **backtest artifact that fails adversarial verification.** This was the last untested lever (the forecast track was already parked with "no edge to stand on"). Combined, the honest conclusion is: **this bot has no demonstrated edge on either the forecast side or the microstructure side, on the first real data we have ever been able to grade.**

## Results (real METAR settlement, both fee regimes)

| Candidate | ran? | ROI opt (0%) | ROI pess (5%) | n_trades | CI-low (pess) | survived refutation? |
|---|---|---|---|---|---|---|
| **#7 distributional arb** | yes | +3.0% | **−0.3%** | 24 | −0.011 | ❌ real inefficiency, **fee eats it** |
| **#6 cross-format** | yes | +0.3% | 0 (0 trades) | 21→0 | 0 | ❌ micro-thin, **fee kills it** |
| **#7b depth-imbalance** | yes | **−45.8%** | −47.8% | 201 | −0.60 | ❌ **anti-signal / noise** |
| **#2 market-making** | yes | **+136.8%** | +136.8% | 206 | +0.958 | ❌ **ARTIFACT — fill-model cheat** |

## Why each is a NO

**#7 distributional arb — the honest near-miss.** A genuine mispricing exists: sum of best-ask across the bucket partition dips below \$1 on a thin tail of city-days, giving +3.0% at zero fee with a *positive* CI-low (+0.022). But it fires on only 24 trades / 4 cities, and the **5% weather fee turns it negative (−0.3%, CI-low −0.011)**. The market's overround (median ask-sum ≈1.057) swamps the ~1% inefficiency. Real, but not tradeable through fees.

**#6 cross-format — same story, thinner.** Only the sum-of-probabilities inconsistency exists (the ladder is a strict single-degree partition, so no literal cross-format overlap). +0.3% at 0% fee → **0 trades survive the 5% fee.** Dead.

**#7b depth-imbalance — falsified.** The one candidate with a predictive (not pure-arb) claim: heavy one-sided resting depth should predict the settle. It predicts the **wrong** direction / is pure noise — **−45.8% ROI over 201 trades across all 20 cities.** This isn't "thin n"; it's a decisive falsification of the imbalance signal.

**#2 market-making — the trap. +137% is a fill-model artifact, not edge.** Adversarial verification of `bakeoff/candidates/c2_market_maker.py:163-183`: the model fills a passive **buy** whenever price *ever* dipped to the bid quote AND a passive **sell** whenever price *ever* lifted to the ask quote, over the same day. Confirmed empirically: **96% of quoted buckets (101/105) booked BOTH a buy and a sell on the same winset** — it captures the full intraday range risk-free, which no real market-maker can (queue priority, cancel/replace races, you never get filled on the good side of 96% of inventory). The P&L is carried by the **sell side winning 80%** (cheap out-of-the-money buckets mostly lose → selling them "wins") filled at prices the market only momentarily touched. Fee-invariance (identical opt/pess) is the maker-fee tell that first drew the eye; the range-capture double-fill is the actual cheat. **Killed** — this is exactly the "fill model assumes you always get the good side" error that burned the shotgun strategy.

## What we proved (so the effort wasn't wasted)

- **The depth-capture logger works** — 331 depth-covered city-days / 1,740 real ladders reached the harness. The infrastructure investment is sound.
- **The harness is now depth-aware and truth-gradeable** — plumbing (`snapshot_replay._SNAPSHOT_COLS` + `decision._FIELDS`) + `run_microstructure_track.py` + the METAR forward-fill are reusable for any future candidate.
- **The gates hold.** No candidate slipped through: the arb died honestly on fees, the predictive signal was falsified on adequate n, and the too-good result was caught as an artifact. The machinery worked as designed.

## Recommendation

**This is the plug-pull.** Both halves of the bake-off are now answered on real, honestly-graded data:
- **Forecast track:** no edge to stand on (parked 06-24; ~3 weeks of data too thin, per-city bias fix overfit).
- **Microstructure track:** no edge either — the only positive-at-zero-fee candidate (#7 arb) is a real but sub-fee inefficiency; the rest are noise or artifacts.

There is no candidate here worth forward-shadowing with capital. The remaining theoretical levers (queue-aware MM modeling, more cities, more weeks of depth) would be chasing a ~1% arb that the fee structure already eats — a bad risk/reward given the do-or-die frame. **Honest call: stop the bot.** If anything is worth keeping alive, it is the *data collection* (the logger is cheap; the depth archive is now the only asset with future option value), not the trading thesis.

*A positive backtest is a forward-shadow candidate, not proof — and here, none even reached forward-shadow. This is a ship-rejection across the board, which is the strong form of the result.*
