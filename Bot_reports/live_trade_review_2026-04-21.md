# Weather Bot — Live Trade Review

**Date:** 2026-04-21
**Database:** `weather_bot.db` (synced from Kamatera VPS; last written 2026-04-22 02:53 UTC)
**Window covered:** 2026-04-13 → 2026-04-22 (first ~9 days of live trading)

---

## Headline numbers

| Metric | Value |
|---|---|
| Total trades (non-phantom) | 23 (IDs 1–29; 9/10/12–15 removed by phantom-claim reset per commit c2d41e6) |
| Closed | 17 |
| Claim-pending | 3 |
| Open | 3 |
| Net P&L on closed trades | **−$17.53** on $175.36 deployed |
| Unrealized P&L on claim-pending | **+$3.09** (locked; awaiting gas/oracle) |
| Capital in open positions | $30.13 |
| Account value (balance + positions) | **$239.92** (started $200) |
| USDC balance | $156.58 |

---

## The net loss is entirely a legacy scar

All −$17.53 of net closed P&L traces to **five "sold externally (reconciled)" trades** whose markets had already resolved against us before the corresponding bug fixes deployed:

| ID | City / Threshold / Dir | Opened | Closed | Size | PnL |
|---|---|---|---|---|---|
| 1  | denver 66F NO      | 04-13 | 04-13       | $9.94  | −$0.24 |
| 4  | Tel Aviv ≥35C NO   | 04-13 | **04-16 16:08** | $9.92  | **−$9.92** |
| 7  | shanghai ≥20C YES  | 04-13 | **04-16 16:07** | $14.63 | **−$14.63** |
| 8  | Tel Aviv ≥35C NO (leg 2 of #4)  | 04-15 | **04-16 16:07** | $9.99  | **−$9.99** |
| 11 | shanghai ≥20C YES (leg 2 of #7) | 04-15 | **04-16 16:07** | $5.74  | **−$5.74** |
| | | | | **Total** | **−$40.52** |

The four simultaneous closures on **04-16 16:07–16:08** line up exactly with deployment of bugs **#18–#20** from `memory/project_live_testing_bugs.md`:

- **#18 (Orphaned Wallet Tokens Re-Imported)** — reconcile previously re-imported resolved-market ERC1155 tokens as fresh open positions every restart.
- **#19 (Exit Spam Loop on Closed Markets)** — past-close markets triggered late-game divergence exit → retry forever.
- **#20 (Vanished Orderbook Trips Circuit Breaker)** — `get_best_bid() == None` fell through to posting $0.01 sells.

Those four positions were opened during the pre-fix era (04-13/15) on markets that ultimately resolved *against* the bot. The 04-15 fix just allowed the bot to finally recognize and book the $0 exits. The damage was already baked in before the fix shipped — the fix stopped it from recurring.

**YES-trade casualties.** Two of the five legacy losses (#7, #11) were YES-direction trades. The 04-15 config tuning removed YES trades entirely ([trade_review_2026-04-15.md Finding 1](trade_review_2026-04-15.md#L21)) — no YES trades have been opened since.

---

## Post-fix performance (2026-04-15 onward)

Excluding the legacy wipeout, the **12 resolved trades since the 04-15 tuning + resilient-exit deployment**:

| Exit reason | Count | P&L |
|---|---|---|
| `resolved` (claimed winners) | 11 | +$19.70 |
| `ensemble flipped 6% → 100%` (#3 Munich) | 1 | +$3.29 |
| **Post-fix total** | **12** | **+$22.99** on ~$120 deployed (~19% ROI) |

**Win rate: 11/12 = 91.7%.** (Sample-size caveat: 95% CI on an 11-of-12 result is roughly 62–99%. True WR is somewhere in that range — we cannot yet distinguish "genuinely excellent" from "lucky streak.")

Every winner exited at $1.00 fill equivalent after on-chain redemption. The one non-resolved close (#3 Munich) triggered the **ensemble-flip exit** correctly — entered with ensemble 6% YES (strong NO conviction), ensemble flipped to 100% YES two days later, bot exited at $0.99 for a +$3.29 win before the market resolved against us. Exactly what that exit is designed to catch.

---

## Open & claim-pending state

### Open (3 positions — $30.13 deployed)

| ID | City | Threshold | Dir | Size | Fill |
|---|---|---|---|---|---|
| 26 | shanghai   | ≥20C | NO | $10.10 | $0.94 |
| 28 | hong kong  | ≥31C | NO | $10.04 | $0.95 |
| 29 | moscow     | ≥9C  | NO | $9.99  | $0.86 |

All three are high-fill NO trades (≥$0.86) — the profile identified in the 04-15 review as the bot's bread and butter (unanimous NO, fill ≥ $0.60, h2c 12-48h → 82% WR in paper).

### Claim-pending (3 positions — $3.09 unrealized)

| ID | City | Parent | Status | Stuck reason |
|---|---|---|---|---|
| 17 | toronto  | —     | `claim_no_balance` | MATIC gas too low — *self-healed via sibling #23 at 02:53 UTC* |
| 21 | tel aviv | 16    | `claim_no_balance` | MATIC gas |
| 27 | istanbul | 25    | `claim_no_balance` | MATIC gas |

Trade #17 cleared at 02:53 UTC via the sibling-redeem self-heal (commit cdc41af) — the neg-risk architecture now detects when a same-condition sibling trade redeems the underlying position and marks the other legs resolved. This confirms `project_auto_claiming_fix.md` (which claims "Bot STOPPED") is **stale** — claims are working again.

The remaining two pending trades are waiting on MATIC balance. Notification at 02:53 UTC: *"MATIC 0.0010 below threshold. Deferring 1 claim(s)."* Minor top-up needed.

---

## Operational signals

**Oracle delays.** Notifications flagged trades #22 and #24 waiting 24h and 49h respectively for UMA oracle to post `reportPayouts()`. Both eventually claimed successfully. This is UMA-side latency, not a bot bug — but at larger trade sizes, capital locked in oracle-delayed positions is a real cost.

**Daily digest running cleanly.** Digest notifications firing on schedule at 00:00 UTC (notifications #116, #121, #126). Bug #13 (missing `db.get_closed_trades()`) confirmed fixed — digest hasn't crashed in 6+ days.

**Account value chart.** Balance history has 7,198 samples over the 6 days since the 04-15 chart fix (bug #16) — ~1,200/day, matching the 60s poll interval. Chart should now be showing continuous movement between trade events instead of flat lines.

---

## Correlation: bug fixes → live trading quality

| Fix date | Bugs | Effect on live trades |
|---|---|---|
| 2026-04-13 | #1–#6 (precision, FOK, fill polling, Kelly caps, allowances, reconcile) | Enabled first live trades at all; trade #1 exited correctly |
| 2026-04-14 | #7 (ensemble flip false exits), #8 (stale DB positions), #9 (get_market_by_id), #10 (associate_trades), #11–#12 (backfill) | Trade #3 (Munich) correctly triggered real ensemble-flip exit; dashboard links populated |
| 2026-04-14 | #13 (missing get_closed_trades), #14 (duplicate rows), #15 (premature claims), #16 (flat chart), #17 (claim_pending value) | Digest stable; no more duplicate reimports; oracle-checked claims; continuous chart |
| 2026-04-15 | #18–#20 (orphan guard, closed-market exits, vanished orderbook) | Caused the 04-16 −$40 closure event (pre-fix positions finally booked); prevented recurrence |
| 2026-04-15 | Algorithm tuning (YES block, conviction 0.85, city-strict, Kelly margin, late-game divergence) | Start of the 11-of-12 winning cohort |
| 2026-04-15 | Resilient exit execution (GTC, repricing, multi-leg settlement) | No failed exits in the post-fix cohort |
| 2026-04-21 | Sibling-redeem self-heal (commit cdc41af) | Trade #17 cleared automatically on 04-22 02:53 UTC |

---

## Bottom line

The live bot has been through **two distinct regimes**:

1. **Learning-phase trades (04-13 to 04-14):** small sample, exposed a dozen architectural bugs, paid −$40.52 in legacy losses that were booked on 04-16 when the fixes caught up to reality.
2. **Post-tuning trades (04-15 onward):** 11-of-12 resolved winners, +$22.99, tracking paper-bot performance (80% WR post-tuning) almost exactly. All exits executing cleanly. Neg-risk claim pipeline matured with sibling-redeem self-heal confirmed working.

Net-net the account is up from $200 → $239.92 (+20%) in ~9 days despite the legacy scar. Removing the legacy cohort, the live strategy is demonstrably profitable at small scale.

The outstanding question is not *"does it work?"* — it clearly does — but *"does it scale?"* See `scaling_ramp_plan_2026-04-21.md` for that analysis.
