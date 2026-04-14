# New Bot Category Research — Polymarket & Kalshi

Research notes on candidate categories for porting the weather bot architecture
to a separate prediction-market bot. Scored 1–5 across five criteria (data
quality, objective resolution, market volume, retail on the other side, model
tractability) for a max of 25.

## What the research found

### Top pick (23/25): Kalshi macro — weekly jobless claims + monthly CPI/NFP/unemployment

- Markets are strike-bucketed like weather ("CPI YoY between 2.7–2.8%",
  "2.8–2.9%"...) — structurally identical to "Denver between 70–72°F". The
  entire decision/sizing/extended-positions architecture ports directly.
- Weekly jobless claims resolve every Thursday 8:30 ET; CPI/NFP/unemployment
  resolve monthly. That's enough cadence to keep a 24/7 scanner busy.
- **The edge fuel:** Cleveland Fed Inflation Nowcasting publishes a
  daily-updated CPI / Core CPI / PCE nowcast that has historically beaten the
  Bloomberg economist consensus. Retail on Kalshi prices from the static
  consensus headline; a bot that re-reads the Cleveland Fed number every
  morning + re-weights gasoline/energy shocks can price every strike bucket
  continuously.
- **Honest caveat:** the headline strikes likely have prop desks on the other
  side — so the edge is probably in the *tail strikes* (far-from-consensus
  buckets) where retail overprices outcomes.
- Kalshi's own Fed research paper cited a jobless-claims market with ~$1.1B
  weekly notional and 39,695 OI — real liquidity.

### Surprise candidate (21/25): Kalshi box office / Rotten Tomatoes markets

- Had this at 18/25 from priors; actual research bumped it. Kalshi runs
  "opening weekend gross > $X" and "Rotten Tomatoes score Monday after release"
  markets on a rolling basis (kalshi.com/category/culture/movies).
- Resolution is fast (3–10 days), data is free (BoxOfficeMojo Thursday
  previews, Fandango advance sales, Deadline trackers, RT review feed), retail
  is extremely tribal and emotional here, and a Bayesian update on incoming
  critic reviews is a real edge.
- Kalshi's own blog
  (news.kalshi.com/p/making-money-with-rotten-tomatoes-movie-markets-kalshi-kit)
  acknowledges traders model these — meaning Kalshi isn't trying to hide that
  it's a data game.
- **Risk:** known manipulation attack surface on RT markets (review-bombing);
  fewer markets per week than macro.

### Honorable mention (20/25): Earthquake / USGS aftershock markets

Fascinating edge — USGS publishes a Reasenberg-Jones aftershock probability
model that retail doesn't read — but event-driven. The scanner sits idle
between mainshocks. Rejected for recurring-volume reasons, but worth noting as
a "burst-mode" bolt-on.

## What the research killed

- **Polymarket earnings markets** — almost suggested these. Research found
  actual volumes: Goldman Q1 = $3,107, JPM = $1,493, Intel = $10.53.
  Untradeable. Trap.
- **AI model release date markets** — no public data feed, pure vibes.
- **CDC flu markets** — couldn't confirm Kalshi has live weekly FluSight-settled
  markets. If they do, this jumps to top-3 because FluSight is a world-class
  public ensemble. Worth rechecking quarterly.
- **Oscars / awards** — not recurring enough, already modeled by sharps.

## Honest take

Two legitimate candidates, and they're actually quite different bots:

1. **Macro bot (CPI + jobless claims + NFP)** is the most faithful port of the
   weather architecture. Same shape (ensemble → calibrated probability over
   strike buckets), same operational tempo (24/7 recalibration as new data
   drops), best recurring market volume. Edge is thinner at the headline and
   lives in the tails. This is the "boring, high-probability-of-working"
   choice.

2. **Box office / RT bot** is more creative, probably has fatter edges because
   retail is wildly more emotional about movies than about CPI, but has lower
   throughput (3–6 major releases a week tops) and a manipulation attack
   surface. This is the "higher edge per trade, fewer trades" choice.

**Caveat to be loud about:** the research subagent got rate-limited by Kalshi
and had to lean on secondary sources for some of the live-market volume
numbers. Before committing to either direction, spend ~30 minutes hitting
Kalshi's public API directly (docs.kalshi.com/api-reference) to pull real 24h
volume, OI, and bid-ask spreads on the specific markets in both categories.
That's the cheap sanity check that tells us whether the edge is theoretical or
actually tradeable at size.

## Full ranking (all candidates scored)

Each criterion scored 1–5, total out of 25.

Legend: **[R]** scored from live research · **[P]** scored from priors (unverified) · **[X]** rejected (with reason)

| Rank | Category | Platform | Data | Resolve | Volume | Retail | Model | Total | Note |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Kalshi macro (CPI + NFP + jobless claims + unemp + PCE) | Kalshi | 5 | 5 | 5 | 3 | 5 | **23** | [R] |
| 2 | Fed rate decisions (FOMC) | Kalshi | 5 | 5 | 3 | 4 | 5 | **22** | [P] different bot shape |
| 3 | Box office / Rotten Tomatoes | Kalshi | 4 | 5 | 3 | 5 | 4 | **21** | [R] |
| 3 | Temperature anomaly markets (monthly/yearly) | Both | 5 | 3 | 3 | 5 | 5 | **21** | [R] weather-adjacent |
| 3 | Cross-platform Kalshi↔Polymarket arb | Both | 5 | 5 | 3 | 4 | 4 | **21** | [X] matching problem, already tried |
| 6 | Hurricane / tropical storms (active) | Kalshi | 5 | 5 | 2 | 3 | 5 | **20** | [P] basically same bot |
| 6 | Natural disasters (earthquakes/wildfires/tornadoes) | Both | 5 | 4 | 2 | 5 | 4 | **20** | [R] thin volume |
| 6 | MLB/NBA/NFL game totals | Both | 5 | 5 | 5 | 1 | 4 | **20** | [X] sharps dominate |
| 9 | Oil/gas EIA weekly inventory | Kalshi | 4 | 5 | 3 | 3 | 4 | **19** | [P] energy specialists on other side |
| 9 | Flight delays | Kalshi | 4 | 5 | 2 | 4 | 4 | **19** | [P] reuses weather data |
| 9 | Crypto BTC/ETH price targets | Polymarket | 4 | 5 | 5 | 2 | 3 | **19** | [X] sharps + options competition |
| 12 | Flu / FluSight hospitalization | Kalshi? | 4 | 4 | 2 | 4 | 4 | **18** | [R] couldn't confirm live markets |
| 12 | Hurricane season counts (May–Nov) | Kalshi | 5 | 3 | 3 | 3 | 4 | **18** | [R] weather-adjacent |
| 12 | Box office opening weekend (originally) | Both | 3 | 5 | 2 | 5 | 3 | **18** | [P] superseded by #3 above |
| 15 | Tech/GitHub milestones | Polymarket | 5 | 3 | 1 | 4 | 4 | **17** | [P] volume too thin |
| 15 | USGS aftershock bursts (post-mainshock only) | Both | 5 | 5 | 1 | 5 | 5 | **21 per event** | [R] scanner idle 95% of time → effectively **17** |
| 17 | Gas prices (AAA / retail) | Kalshi? | 4 | 4 | 2 | 3 | 3 | **16** | [P] uncertain market existence |
| 17 | Polymarket company earnings | Polymarket | 5 | 5 | 1 | 4 | 1 | **16** | [X] volumes $10–$3K, untradeable |
| 19 | Nielsen TV ratings | Kalshi? | 3 | 4 | 1 | 4 | 3 | **15** | [P] market existence uncertain |
| 20 | AI model release dates (GPT-5.5, Claude 5, etc.) | Polymarket | 1 | 3 | 4 | 5 | 1 | **14** | [X] no data feed, pure vibes |
| 20 | Elections / polling aggregates | Both | 3 | 1 | 5 | 3 | 2 | **14** | [X] long horizon, heavily analyzed |
| 20 | Oscars / Eurovision / awards | Both | 3 | 5 | 1 | 3 | 2 | **14** | [X] not recurring, sharps model them |

Total headcount is ~22 categories. The max score of 25 is the criterion
ceiling (5 × 5), not the number of candidates.

## Key honest caveats on the scoring

1. **[R] rows are grounded in actual market inspection this session.** The
   research agent hit Polymarket directly and verified volume on several
   specific markets. Kalshi's API rate-limited the agent, so Kalshi scores
   lean on secondary sources (Kalshi's own blog, the Fed research paper,
   aggregators). The **#1 macro pick's volume score of 5** is the number to
   verify first — confirm strike-bucket depth against Kalshi's API directly
   before committing.

2. **[P] rows are prior estimates and should be treated as hypotheses, not
   facts.** Several (flight delays, gas prices, Nielsen, GitHub milestones)
   hinge on whether Kalshi actually lists those markets in tradeable volume
   — the research agent didn't drill in because they didn't clear the
   threshold to be worth it.

3. **The #2 FOMC row (22) is deceptive.** It scores high but it's a
   fundamentally *different* bot shape than the weather architecture —
   near-arbitrage against CME FedWatch rather than ensemble forecasting.
   Porting weather code doesn't buy much there; the decision layer would
   need a rewrite. Deprioritize unless the near-arb angle specifically
   appeals.

4. **The two new entrants that genuinely moved rankings vs. priors:**
   - **#1 Kalshi macro jumped from 21 → 23** after research found the
     Cleveland Fed Inflation Nowcasting feed + confirmed market liquidity via
     the Kalshi Fed paper. The daily-updated nowcast is the missing
     "ensemble" analog previously missing from the analysis.
   - **#3 Box office / Rotten Tomatoes jumped from 18 → 21** after research
     found Kalshi actively promotes these markets on their own blog and RT
     score markets have a tight, tractable Bayesian update model on incoming
     reviews. Had underweighted retail tribalism.

5. **#1 and #3 (box office) are legitimately different bets.** #1 is "boring,
   high confidence of working, thinner edge per trade, lots of trades." #3 is
   "fatter edge per trade, fewer trades, more attack-surface risk from
   review-bombing." They're not mutually exclusive — the architecture ports
   to both.
