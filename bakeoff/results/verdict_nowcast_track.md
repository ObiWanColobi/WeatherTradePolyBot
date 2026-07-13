# Verdict — Candidate #8: intraday conditional-max nowcast (2026-07-12)

**RESULT: NO EDGE (net loss once settled correctly). Headline +95% was a settlement ARTIFACT,
not a real result — caught on adversarial verification. Additionally uncovered a harness bug:
the scorer mis-settles °C-exact bucket markets.**

## What was built

The last untested structural idea from the 2026-07-12 research: use the REAL-TIME observed
morning temperature trajectory (not a forecast, not just the order book) to price the buckets.

- `bakeoff/candidates/c8_intraday_nowcast.py` — at a midday-local fire time, reconstructs the
  running-max temp so far and a tune-learned "remaining warming" distribution by local hour,
  prices each bucket, and buys/sells the single best mispriced bucket-side after fees, walking
  REAL captured depth via `cost_fill`/`make_trade`.
- `bakeoff/run_nowcast_track.py` — fires at a LOCAL CLOCK HOUR (default 12:00 local), not
  hours-before-close (the 12h-before-close snapshot lands ~3–7am local = pre-dawn, no signal);
  reconstructs the running max lookahead-safely from `weather_obs` (same source/obs_type as
  settlement truth); trains the warming model only on local-days strictly BEFORE each scored
  day (leak-free).

## What checks PASSED (the design is honest where it counts)

- **Running-max convergence:** end-of-day running max == settled `truth_f` on 140/140 city-days
  (0 mismatches) → no phantom edge from a richer data source than the one we grade against.
- **No lookahead in the signal:** traced ankara 2026-06-27 — at 11am local the model saw only
  up to 75.2°F on a clean 55→75 morning ramp; the peak (82.4) came late afternoon. Correct.
- **Depth is real, not synthetic:** the sold buckets had genuine captured 10-level bid ladders
  (e.g. 500–729 shares of real depth); this is NOT the thin/synthetic-book problem, and NOT the
  double-booking cheat (all pure one-side trades).

## Why the headline was a MIRAGE (adversarial verification)

The runner reported **80/81 wins, +95%/+95% (opt/pess), roi 0.0** (the `roi 0.0` itself was a
tell — the scorer's `roi = pnl/cost if cost>0 else 0.0` guard zeroes out when trades are net
sells with a credit basis). An 80/80 win rate is the signature of the artifact that killed
candidate #2 and shotgun, so it got hunted, not trusted.

**Independent re-settlement (each trade vs its OWN winset + truth, ignoring the scorer):**
- **43 of 81 trades were settled WRONG by the harness scorer.**
- Honest tally: **37/81 wins, total P&L = −$148.97** → the strategy LOSES.

**Root cause — harness bug on °C-exact buckets.** Ankara/Beijing/Istanbul etc. list °C-exact
buckets (`bound_lo_f == bound_hi_f`, title like `28°C`). `shotgun.bets.compute_winset` maps each
such bucket to a **2°F-wide integer win-set** (`integer_fs_for_exact_c` → e.g. 82.4°F → [82,83]).
Consecutive °C buckets (82.4, 84.2, …) thus produce OVERLAPPING/misaligned integer sets that do
not cleanly tile the integer °F line, and settlement rounds the truth to a single integer that
lands in the wrong set. Concretely: ankara 06-27 sold the `28°C`/[82,83] bucket (market bid 0.62,
correctly — truth 82.4 makes it the WINNING bucket), yet the scorer recorded `payout=0, pnl=+31`
(treated it as resolving NO). That inversion, repeated across 43 trades, manufactured the +95%.

## Verdict

- **Candidate #8 shows NO tradeable edge** — it loses money once settled honestly, consistent
  with the LOW prior (the intraday obs game is crowded by SaaS feeds + second-scale bots).
- **The `roi 0.0` guard + the 80/80 win rate were the alarms that worked** — [[feedback_backtest_validates_rejection_not_acceptance]] and the "always get the good side" reflex from shotgun/#2 caught it.
- **Harness caveat (real, but does NOT rescue the candidate):** the °C-exact-bucket settlement
  bug means the scorer is unreliable for °C-listed cities. Even so, the INDEPENDENT settlement
  (which is correct) says the candidate loses. Fixing the scorer would not turn −$149 positive;
  it would only remove the fake win. This bug should be filed against the shared harness because
  it silently corrupted a candidate result — but it changes the sign of the artifact, not the
  verdict.

## Status

Plug-pull call stands. All four microstructure candidates + the forecast track + WeatherNext 2 +
now the intraday nowcast → no tradeable edge on any track. Candidate #8 and its runner are kept
local-only (research artifacts); no trading code changed. If the harness is ever reused, the
°C-exact-bucket settlement bug must be fixed first.
