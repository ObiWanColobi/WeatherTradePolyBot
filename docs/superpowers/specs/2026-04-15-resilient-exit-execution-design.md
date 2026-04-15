# Resilient Exit Execution Design

**Date:** 2026-04-15
**Status:** Draft
**Goal:** Replace the current 2-attempt FOK exit execution with GTC orders managed across bot cycles, using book-aware repricing until filled.

---

## Problem

The current exit execution in `executor/live.py:close_full()` posts a FOK sell at `mid - $0.01`. If rejected (thin book), it retries once at `$0.01`. If both fail, the position is left open with a "manual intervention needed" log.

On late-game weather markets, liquidity dries up as prices approach binary (0 or 1). FOK orders demand an instant full fill — exactly what a thin book can't provide. The result: exit signals fire correctly but we can't actually get out.

## Solution

Replace FOK sells with GTC (Good-Til-Cancelled) limit orders that persist on the CLOB order book. Each bot cycle, check if the order filled. If not, read the current best bid and reprice to undercut it. This lets us:

- Sit on the book and wait for a counterparty instead of demanding instant fills
- React to changing liquidity each cycle
- Guarantee eventual exit by walking price down to $0.01 if needed

---

## Design

### New Trade State: `exit_pending`

A new status value between `open` and `closed`. A trade in `exit_pending` has a live GTC sell order on the CLOB that hasn't filled yet.

New fields on the **parent** trade row (no new DB table):

| Field | Type | Description |
|---|---|---|
| `exit_order_id` | str | CLOB order ID of the pending GTC sell |
| `exit_order_price` | float | Current listed price of the GTC order |
| `exit_order_placed_at` | str (ISO) | Timestamp of the most recent order post/reprice |

These fields are only populated while status is `exit_pending`. Cleared on settlement.

### Exit Execution Flow

#### Initial trigger (exit signal fires, no pending order yet)

1. Gather all legs for the position (`db.get_position_legs(parent_id)`)
2. Sum `shares` across all legs to get total position size
3. Get current best bid from the CLOB
4. Post a GTC sell at `best_bid - $0.01` (undercut to be first in line)
   - If no bids exist, post at `$0.01`
5. Store `exit_order_id`, `exit_order_price`, `exit_order_placed_at` on the parent trade row
6. Set trade status to `exit_pending` on all legs
7. Store the exit reason on all legs (so we don't lose why we exited)

#### Subsequent cycles (trade has `exit_pending` status)

Handled in a new **Phase 1** of the exit pass, before evaluating new exit signals:

1. Query CLOB order status — did the GTC order fill?
2. **Filled:** Settle the position (see Settlement section below)
3. **Not filled:**
   a. Get current best bid
   b. If our listed price is above best bid → cancel order, repost at `best_bid - $0.01`
   c. If our listed price is at or below best bid → leave it (we're already competitive)
   d. If no bids exist → cancel and repost at `$0.01`
   e. Update `exit_order_id`, `exit_order_price`, `exit_order_placed_at`
4. Skip this trade in Phase 2 (don't re-evaluate exit signals)

#### Edge cases

| Scenario | Behavior |
|---|---|
| No midpoint / no book at all | Post at `$0.01` — same as current last-resort |
| Cancel fails (order filled between check and cancel) | Treat as filled, proceed to settlement |
| Bot restarts with `exit_pending` trades | Phase 1 picks them up on the next cycle — GTC order is still live on CLOB |
| CLOB API error during reprice | Log warning, skip reprice this cycle, retry next cycle |
| Partial fill (GTC partially matched) | Query remaining size, cancel remainder, repost for unfilled shares |

### Settlement (Multi-Leg)

When the GTC order fills, we have one fill price and one total proceeds amount for the combined shares of all legs.

**Allocation:** Distribute proceeds proportionally by each leg's share count.

Example: Leg A has 30 shares, Leg B has 20 shares, total = 50 shares.
- Leg A gets 60% of proceeds, Leg B gets 40%
- Both record the same `exit_price`
- Each leg calculates its own PnL based on its original `fill_price`

Settlement steps:
1. Query fill details from CLOB (price, shares, fee)
2. For each leg:
   - `exit_price = fill_price_from_clob`
   - `proceeds = leg_shares / total_shares * total_proceeds`
   - `pnl = proceeds - leg_cost - (leg_shares / total_shares * fee)`
   - Update trade row: `exit_price`, `closed_at`, `status = "closed"`, `pnl`, `pnl_pct`, `exit_reason`, `fee_usdc`
   - Clear `exit_order_id`, `exit_order_price`, `exit_order_placed_at`
3. Credit total proceeds to balance
4. Send close notification

### Bot Cycle Integration

`run_exit_pass()` in `weather_bot.py` becomes two phases:

```
Phase 1: Manage pending exit orders
  - Loop through trades with status == "exit_pending"
  - Check fill, reprice if needed, settle if filled

Phase 2: Evaluate new exit signals (existing logic)
  - Skip any trade already in exit_pending
  - When should_exit == True, initiate the GTC flow instead of calling close_position()
```

### Config

One new key in the `WEATHER` config dict:

```python
"exit_reprice_min_step":  0.01,   # minimum price drop per reprice cycle
```

### Best Bid Source

The existing `polymarket.get_orderbook(token_id)` returns `{"bids": [...], "asks": [...]}`. We add a thin helper `get_best_bid(token_id)` that calls `get_orderbook()`, sorts bids descending by price, and returns the highest bid price (or `None` if no bids). This is the only new function in `markets/polymarket.py`.

### GTC Order Support

The current `_post_order()` method hardcodes `OrderType.FOK` for sells. This needs a parameter to select `OrderType.GTC` instead. The py-clob-client SDK supports both — `OrderType.GTC` is available in the same `OrderType` enum.

No changes to the buy path — entries remain FOK.

### Partial Fills

GTC orders can partially fill. When we detect a partial fill during a management cycle:

1. Settle the filled portion immediately — allocate proceeds proportionally across legs, reduce each leg's `shares` by its proportion of the filled amount
2. Cancel the remaining order
3. Repost a new GTC for the unfilled shares at the current best bid - $0.01
4. Update `exit_order_id` to the new order

This avoids leaving filled proceeds in limbo while waiting for the remainder.

---

## File Map

| File | Changes |
|---|---|
| `config.py` | Add `exit_reprice_min_step` key |
| `markets/polymarket.py` | Add `get_best_bid(token_id)` helper using existing `get_orderbook()` |
| `executor/live.py` | Add GTC support to `_post_order()`, new `initiate_exit()` and `manage_pending_exit()` methods, update `close_full()` to use new flow |
| `weather_bot.py` | Add Phase 1 (pending exit management) to `run_exit_pass()`, route exit signals through new initiation flow |
| `db.py` | Add `exit_order_id`, `exit_order_price`, `exit_order_placed_at` fields, `exit_pending` status support, helper to query pending exits |

---

## What This Does NOT Change

- **Exit decision logic** (`weather_exit.py`) — untouched. The triggers for when to exit remain exactly the same.
- **Entry execution** — entries remain FOK. This only affects sells.
- **Paper executor** — paper mode doesn't hit real CLOB, so it continues using instant simulated fills.
- **Dashboard** — `exit_pending` status will need to be handled in display (show as "exiting..." or similar), but this is cosmetic and can be a follow-up.
