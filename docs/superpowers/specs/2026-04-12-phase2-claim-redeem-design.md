# Phase 2: On-Chain Claim/Redeem for Resolved Markets

**Date:** 2026-04-12
**Branch:** `live`
**Depends on:** Phase 1 (LiveExecutor, complete)

---

## Problem

When a market resolves, `settle_resolved()` credits winnings to the DB balance immediately — but never actually redeems the conditional tokens on-chain. Winning shares sit in the wallet unredeemed. The DB says we have the money, but the USDC hasn't actually been claimed from the Polymarket CTF contract.

## Goal

After resolution detection, automatically redeem winning conditional tokens on-chain and only credit the DB balance once the claim transaction is confirmed on Polygon. Losing trades close immediately (no shares to redeem).

---

## Approach

**Direct web3.py calls to the Conditional Tokens Framework (CTF) contract on Polygon.** The py-clob-client SDK has no claim/redeem endpoints, so we interact with the CTF contract directly using web3.py (already a transitive dependency of py-clob-client). This mirrors the pattern in `scripts/setup_allowances.py`.

---

## Settlement Flow (Revised)

### Current (Phase 1)
```
Resolution detected → credit balance → close trade (all in one step)
```

### New (Phase 2)
```
Resolution detected (CLOB midpoint confirms binary outcome)
    │
    ├─ LOSING trade:
    │   → Set exit_price=0.00, pnl, status='closed' immediately
    │   → No on-chain action needed (worthless shares)
    │
    └─ WINNING trade:
        → Record resolution: actual_resolution, forecast_correct, exit_price=1.00
        → Set claim_status='claim_pending'
        → Do NOT credit balance, do NOT set status='closed'
        │
        [Next bot loop — claims pass]
        → Check MATIC balance (gas guard)
        → Call CTF redeemPositions() on Polygon
        → Poll for tx receipt
        │
        ├─ Success:
        │   → Credit DB balance with proceeds
        │   → Set status='closed', claim_status='claim_confirmed'
        │   → Record claim_tx_hash
        │
        └─ Failure:
            → Increment claim_retries, record claim_last_attempt
            → Backoff until next attempt (configurable schedule)
            → After max retries: claim_status='claim_failed', log warning
```

---

## New DB Columns on `trades`

| Column | Type | Purpose |
|--------|------|---------|
| `claim_status` | TEXT | `NULL` (paper/not applicable) · `claim_pending` · `claim_confirmed` · `claim_failed` |
| `claim_tx_hash` | TEXT | Polygon transaction hash of the redemption |
| `claim_retries` | INTEGER | Number of claim attempts so far (default 0) |
| `claim_last_attempt` | TEXT | ISO timestamp of the most recent claim attempt |

**Note:** Losing trades and paper trades will have `claim_status = NULL`. Only winning live trades go through the claim flow.

---

## New Module: `chain/claimer.py`

Encapsulates all web3/CTF interaction. Stateless — receives parameters, returns results.

### Responsibilities

1. **Initialize web3 provider** — connect to Polygon RPC (configurable endpoint)
2. **Load CTF contract** — ABI for `ConditionalTokens` at `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
3. **`claim_winnings(condition_id, index_sets) → tx_hash`** — build, sign, and submit `redeemPositions()` transaction
4. **`check_tx_status(tx_hash) → "confirmed" | "pending" | "failed"`** — poll receipt
5. **`get_matic_balance() → float`** — check gas token balance
6. **Nonce management** — fetch nonce at tx time, no local nonce cache (single-threaded bot, one tx at a time)

### Contract Call Details

The CTF `redeemPositions()` function signature:
```solidity
function redeemPositions(
    IERC20 collateralToken,   // USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    bytes32 parentCollectionId, // bytes32(0) for top-level conditions
    bytes32 conditionId,       // from the market's condition_id
    uint256[] indexSets        // [1] for YES wins, [2] for NO wins
)
```

- **Collateral token:** USDC on Polygon (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
- **Parent collection ID:** `bytes32(0)` (top-level, no nested conditions)
- **Condition ID:** Stored per-trade (already available from market metadata)
- **Index sets:** `[1]` to redeem YES tokens (trade direction=YES, resolved YES), `[2]` to redeem NO tokens (trade direction=NO, resolved NO). Matches the token type held, not the resolution outcome directly.
- **Gas:** Polygon gas is ~0.001 MATIC per claim tx

### Dependencies

- `web3.py` (already installed as transitive dep of py-clob-client)
- CTF contract ABI (JSON file shipped in `chain/abi/conditional_tokens.json`)
- Polygon RPC endpoint (configurable, default: public Polygon RPC)

---

## Changes to `executor/live.py`

### `settle_resolved(trade, resolved_yes)` — Modified

**Losing trades:** Unchanged — immediate close with $0 proceeds, `claim_status=NULL`.

**Winning trades:** No longer credits balance or sets `status='closed'`. Instead:
- Records resolution metadata (actual_resolution, forecast_correct, exit_price, resolution_price)
- Sets `claim_status='claim_pending'`, `claim_retries=0`
- Logs: `[settle] Trade #{id} won — claim pending`

### `process_pending_claims()` — New Method

Called each bot loop iteration. Processes all trades with `claim_status='claim_pending'`:

1. Query DB for pending claims
2. For each pending claim:
   a. Check if enough time has elapsed since `claim_last_attempt` (backoff schedule)
   b. Check MATIC balance (gas guard — skip if below `claim_min_matic_balance`)
   c. Call `claimer.claim_winnings(condition_id, index_sets)`
   d. Poll for tx confirmation (with timeout)
   e. On success: `db.update_balance(proceeds)`, update trade to `status='closed'`, `claim_status='claim_confirmed'`, record `claim_tx_hash`
   f. On failure: increment `claim_retries`, record `claim_last_attempt`, log warning
   g. If `claim_retries >= len(backoff_schedule)`: set `claim_status='claim_failed'`, log loudly

### Paper executor — Unchanged

`PaperExecutor.settle_resolved()` keeps current behavior (immediate close + balance credit). No claim flow. `process_pending_claims()` is a no-op on the base class.

---

## Bot Loop Integration

New claims pass added to `weather_bot.py` main loop, after the resolution pass:

```
Exit pass
Calibration pass
Resolution pass        ← now sets claim_pending for winning live trades
Claims pass (NEW)      ← process_pending_claims()
Risk check
Entry pass
Extended positions pass
```

The claims pass runs every loop iteration (~60s). Backoff timing is enforced inside `process_pending_claims()` by comparing `claim_last_attempt` against the backoff schedule — if not enough time has passed, the claim is skipped for this cycle.

---

## Configuration

New keys added to `WEATHER` dict in `config.py`:

```python
# ── On-chain claim/redeem ────────────────────────────────────────────────
"claim_retry_backoff_minutes":  [5, 30, 120, 480, 1440],  # retry schedule (max retries = len)
"claim_min_matic_balance":      0.01,                       # skip claims if MATIC below this
"polygon_rpc_url":              "https://polygon-rpc.com",  # Polygon RPC endpoint
```

- **`claim_retry_backoff_minutes`**: List of minutes to wait between retries. Length of list = max retries (5). Schedule: 5min → 30min → 2hr → 8hr → 24hr. Total window: ~35 hours.
- **`claim_min_matic_balance`**: Gas guard threshold. Below this, defer claims and log warning. Does not consume a retry.
- **`polygon_rpc_url`**: Polygon JSON-RPC endpoint. Default is public; can be swapped for Alchemy/Infura for reliability.

---

## Condition ID Tracking

The `redeemPositions()` call requires the market's `condition_id`. In Polymarket, the condition_id IS the market_id — they are the same identifier. The `market_id` column already stored on every trade in the `trades` table is the condition_id needed for on-chain redemption.

**No new column needed.** The existing `market_id` field serves double duty.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| MATIC too low | Defer claim (no retry consumed), log warning |
| RPC timeout / network error | Consume retry, backoff |
| Transaction reverted | Consume retry, backoff, log tx hash + revert reason |
| Nonce collision | Fetch fresh nonce, retry immediately (no retry consumed) |
| Max retries exhausted | `claim_failed` status, log loudly, visible in dashboard |
| Manual recovery | Set `claim_retries=0` and `claim_status='claim_pending'` in DB to re-trigger |

---

## Dashboard Visibility

Minimal changes — leverage existing open/closed position display:

- Winning trades in `claim_pending` state show as **open** (not yet closed) with a "Claim Pending" badge
- `claim_failed` trades show a **"Claim Failed"** warning badge in the positions table
- No new dashboard pages — just status visibility in existing tables

---

## Files Changed

| File | Change |
|------|--------|
| `chain/__init__.py` | New — empty package init |
| `chain/claimer.py` | New — web3 CTF interaction (claim, tx status, MATIC balance) |
| `chain/abi/conditional_tokens.json` | New — CTF contract ABI |
| `executor/live.py` | Modified — split settle_resolved, add process_pending_claims() |
| `executor/base.py` | Modified — add process_pending_claims() default (no-op) |
| `weather_bot.py` | Modified — add claims pass to bot loop |
| `db.py` | Modified — new columns (claim_status, claim_tx_hash, claim_retries, claim_last_attempt) |
| `config.py` | Modified — add claim config keys |
| `tests/test_claimer.py` | New — unit tests for chain/claimer.py (mocked web3) |
| `tests/test_claim_flow.py` | New — integration tests for claim lifecycle (pending → confirmed/failed) |

---

## What This Phase Does NOT Include

- **Dashboard wallet balance display** (MATIC + USDC) — separate feature, to be designed independently
- **Email/notification alerts** for claim failures — may or may not be built separately
- **Emergency kill switch** — Phase 3
- **Slippage kill-switch** — Phase 3
- **Position reconciliation from on-chain state** — future phase (currently DB is source of truth)
