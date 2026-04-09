# Extended Positions (Scale-In) Design

**Date:** 2026-04-08
**Status:** Approved

## Overview

Add the ability to scale into winning positions by adding legs when meteorological ensemble conviction holds or strengthens over time. Closer-to-resolution forecasts are more reliable — if the ensemble maintains its stance as the forecast window narrows, that is genuinely new information justifying additional capital.

Implemented as a separate `run_extended_positions_pass()` in the bot loop, fully isolated from entry and exit logic. Disabled via config toggle.

## Design Decisions

1. **Trigger: ensemble conviction stability over time** — not price-based. Insufficient post-fix data (7 trades, 0 resolved as of 2026-04-08) to justify price-improvement or market-confirmation conditions. Revisit after 30+ resolved post-fix trades.
2. **Approach: separate pass (Approach B)** — clean isolation from entry and exit logic. Own function, own eligibility checks, easy to toggle off.
3. **Exit: all legs close together** — any exit trigger on any leg closes the entire extended position. One thesis, one exit.
4. **Sizing: fresh Kelly per leg** — re-run `kelly_size()` with current inputs. Later legs naturally size larger due to reduced horizon discount.
5. **UI: expander pattern** — parent row shows aggregate stats, expandable to show individual leg detail.

## Config

```python
"extended_positions_enabled": True,           # master toggle — False skips pass entirely
"extended_positions_max_add_ons": 2,          # max add-on legs (3 total with initial entry)
"extended_positions_leg_spacing_hours": 12.0, # time-to-close band spacing per leg
"extended_positions_min_cooldown_hours": 12.0 # minimum hours between any two legs
```

**Time band calculation** (counting backwards from market close):
- Add-on 1 eligible when: `hours_to_close < (max_add_ons) * leg_spacing_hours` → default <24h
- Add-on 2 eligible when: `hours_to_close < (max_add_ons - 1) * leg_spacing_hours` → default <12h

Generalizes to N add-ons: add-on K eligible when `hours_to_close < (max_add_ons - K + 1) * leg_spacing_hours`.

**Cooldown:** Minimum `extended_positions_min_cooldown_hours` (default 12h) must elapse since the most recent leg's `opened_at`. Prevents immediate add-ons when initial entry lands inside an already-open time band.

## Data Model

### trades table changes

Two new columns:

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `parent_trade_id` | INTEGER | NULL | NULL for initial entries. References `trades.id` for add-on legs. |
| `leg_number` | INTEGER | 1 | 1 = initial entry, 2 = first add-on, 3 = second add-on. |

No new tables. Each leg is a full trade record with its own fill_price, size_usdc, shares, ensemble snapshot, edge_score, opened_at, etc.

### New DB helper functions (in db.py)

- `get_position_legs(parent_id: int) -> list[dict]` — returns all legs for a position. Queries `WHERE id = parent_id OR parent_trade_id = parent_id`, ordered by leg_number. Includes the parent itself.
- `get_leg_count(parent_id: int) -> int` — count of open legs for a position (same WHERE clause).
- `get_latest_leg(parent_id: int) -> dict` — most recent leg by opened_at (for cooldown + ratchet checks). Same WHERE clause, ordered by opened_at DESC, limit 1.

## Extended Positions Pass

New function `run_extended_positions_pass()` in `weather_bot.py`. Called after `run_entry_pass()`, before `run_exit_pass()`. Skipped entirely if `extended_positions_enabled` is False.

### Eligibility checks (per open parent trade)

Evaluated in order. Any failure skips the trade.

1. **Leg cap** — `get_leg_count(trade_id) >= max_add_ons + 1` → skip (already at max legs).

2. **Time band** — Determine which add-on number this would be (current leg count). Check `hours_to_close < (max_add_ons - add_on_index + 1) * leg_spacing_hours`. Not in the band yet → skip.

3. **Cooldown** — `hours_since_last_leg >= min_cooldown_hours` using `get_latest_leg()`. Too recent → skip.

4. **Ensemble ratchet** — Fetch current ensemble from a fresh scan. Compare to the previous leg's entry ensemble:
   - NO bet: current `ens_yes / ens_n` must be ≤ previous leg's value (fewer YES votes = stronger NO conviction).
   - YES bet: current `ens_yes / ens_n` must be ≥ previous leg's value (more YES votes = stronger YES conviction).
   - Any weakening since last leg → skip.

5. **Kelly sizing** — `kelly_size()` with current model_prob, current market_price, current days_to_resolution. Standard calculation, no special treatment for add-ons.

6. **CLOB simulation + slippage** — same as normal entry. If slippage exceeds `entry_max_slippage_pct`, attempt size reduction (75%, 50%, 25%).

7. **Exposure cap** — total exposure across all positions (including all legs) must remain under `decision_max_exposure_pct`.

8. **Execute** — `place_order()` creates a new trade record with `parent_trade_id` and `leg_number` set.

### Not re-checked for add-ons

The initial entry already passed these. Add-on eligibility is about conviction stability, not re-qualifying the market:
- Entry edge % threshold
- Bid-ask spread
- Volume floor
- Minimum fill price
- Trader consensus

## Exit Logic Changes

### All-legs-together exit

When any exit trigger fires (ensemble flip, adverse price move, spread dry-up) on any trade that is part of an extended position:

- If trigger is on the parent (`parent_trade_id IS NULL`): close parent + all children via `get_position_legs(trade.id)`.
- If trigger is on a child (`parent_trade_id IS NOT NULL`): close parent + all siblings via `get_position_legs(parent_trade_id)`.

Each leg gets its own `close_full()` call with P&L calculated from its individual fill_price and shares. All legs share the same `exit_reason`.

### Adverse price move

Evaluated per-leg against each leg's own fill_price. If ANY leg triggers the adverse threshold, all legs close. The earliest/worst-priced leg acts as the tripwire.

### Resolution (settle_resolved)

No change needed. Each leg is its own trade record. All legs on the same market resolve at the same time naturally since they share the same `market_id` and `end_date`.

## Dashboard UI Changes

### Open Positions table

Non-extended trades: no change.

Extended positions:
- **Parent row** shows aggregate stats: total size_usdc across all legs, weighted average fill_price, total shares, combined unrealized P&L, leg count display (e.g., "2/3 legs").
- **Expander** below parent row: `st.expander("Show N legs")` reveals a sub-table with per-leg detail: leg number, fill_price, size_usdc, shares, opened_at, ensemble at entry, individual unrealized P&L.

### Closed Positions table

Same pattern. Parent row shows aggregate P&L and blended stats. Expander shows per-leg breakdown.

### Per-trade close button

Closing any leg of an extended position triggers close-all. Confirmation dialog notes total legs: "Close all 3 legs of Seoul NO position?"

### No changes to

Scanner table, risk banner, analytics charts. These operate at the trade-record level and naturally include leg data.

## Future Considerations (post 30+ resolved trades)

- Re-evaluate whether add-on trigger should incorporate price conditions (market divergence for cheaper entry, or market confirmation for momentum signal).
- Adjust default leg_spacing_hours and cooldown based on observed add-on performance.
- Consider Option 2 UI (inline indented child rows) if expander pattern feels clunky in practice.
