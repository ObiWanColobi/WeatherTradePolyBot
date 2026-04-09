# Extended Positions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scale-in capability so the bot can add legs to existing positions when ensemble conviction holds or strengthens as time-to-close narrows.

**Architecture:** Separate `run_extended_positions_pass()` in the bot loop (after entry, before exit). Each leg is a full trade record linked by `parent_trade_id`. All legs of a position exit together. Dashboard shows parent aggregate with expandable leg detail.

**Tech Stack:** Python, SQLite, Streamlit

**Spec:** `docs/superpowers/specs/2026-04-08-extended-positions-design.md`

---

### Task 1: Config Keys

**Files:**
- Modify: `config.py:15-111` (add to WEATHER dict)

- [ ] **Step 1: Add extended positions config keys**

Add these keys to the WEATHER dict in `config.py`, after the risk config block (after line ~111):

```python
    # ── Extended positions (scale-in) ─────────────────────────────────────────
    "extended_positions_enabled":          True,    # master toggle — False skips pass entirely
    "extended_positions_max_add_ons":      2,       # max add-on legs (3 total with initial entry)
    "extended_positions_leg_spacing_hours": 12.0,   # time-to-close band spacing per leg
    "extended_positions_min_cooldown_hours": 12.0,  # minimum hours between any two legs
```

- [ ] **Step 2: Commit**

```bash
git add config.py
git commit -m "feat(config): add extended_positions config keys"
```

---

### Task 2: DB Schema Migration + Helper Functions

**Files:**
- Modify: `db.py:168-205` (add columns in migration block)
- Modify: `db.py` (add three new query functions)

- [ ] **Step 1: Add schema migration columns**

In `db.py`, after line 188 (the `resolution_attempts` migration), add:

```python
        _safe_add_column(conn, "trades", "parent_trade_id",  "INTEGER")
        _safe_add_column(conn, "trades", "leg_number",       "INTEGER DEFAULT 1")
```

- [ ] **Step 2: Add helper functions**

Add these three functions in `db.py`, after the existing `get_open_market_ids()` function (after line ~389):

```python
def get_position_legs(parent_id: int) -> list[dict]:
    """Return all legs of an extended position (parent + children), ordered by leg_number."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) AND status = 'open' "
            "ORDER BY leg_number",
            (parent_id, parent_id),
        ).fetchall()
        return [dict(r) for r in rows]


def get_leg_count(parent_id: int) -> int:
    """Count open legs for an extended position (parent + children)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE (id = ? OR parent_trade_id = ?) AND status = 'open'",
            (parent_id, parent_id),
        ).fetchone()
        return row[0] if row else 0


def get_latest_leg(parent_id: int) -> dict | None:
    """Return the most recently opened leg of an extended position."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) AND status = 'open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (parent_id, parent_id),
        ).fetchone()
        return dict(row) if row else None
```

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat(db): add parent_trade_id, leg_number columns and position leg queries"
```

---

### Task 3: Extended Positions Pass

**Files:**
- Create: `weather_extended.py`

This is the core logic — a standalone module that evaluates open parent trades for add-on eligibility.

- [ ] **Step 1: Create `weather_extended.py`**

```python
"""
Extended Positions Pass
────────────────────────
Evaluates open positions for scale-in add-on legs. Runs after entry pass,
before exit pass. Skipped entirely when extended_positions_enabled is False.

Eligibility (all must pass):
  1. Leg cap      — position hasn't reached max legs
  2. Time band    — hours_to_close is within the correct band for this add-on
  3. Cooldown     — minimum hours since last leg
  4. Ens ratchet  — ensemble conviction equal or stronger than previous leg
  5. Kelly sizing — fresh Kelly with current inputs
  6. CLOB sim     — slippage within threshold
  7. Exposure cap — total portfolio exposure within limit
"""
from datetime import datetime, timezone

import db
from config import WEATHER
from weather_sizing import kelly_size
from markets.polymarket import simulate_fill_for_token

_ENABLED        = WEATHER.get("extended_positions_enabled", True)
_MAX_ADD_ONS    = WEATHER.get("extended_positions_max_add_ons", 2)
_LEG_SPACING_H  = WEATHER.get("extended_positions_leg_spacing_hours", 12.0)
_MIN_COOLDOWN_H = WEATHER.get("extended_positions_min_cooldown_hours", 12.0)
_MAX_EXPOSURE   = WEATHER.get("decision_max_exposure_pct", 0.90)
_MAX_SLIPPAGE   = WEATHER.get("entry_max_slippage_pct", 0.05)


def check_extended_position(trade: dict, current_scan: dict | None) -> dict | None:
    """
    Evaluate whether an open parent trade qualifies for an add-on leg.

    Args:
        trade:         open parent trade row from DB (parent_trade_id IS NULL)
        current_scan:  fresh scan result for this market from the weather layer,
                       or None if scan unavailable. Must contain:
                       - ens_yes, ens_n, ens_pct (current ensemble)
                       - model_prob, market_price (for Kelly)
                       - hours_to_close, days_to_resolution

    Returns:
        dict with add-on details if eligible, or None if not eligible.
        Dict keys: size_usdc, direction, model_prob, market_price, ens_yes, ens_n,
                   reason_eligible, leg_number
    """
    if not _ENABLED:
        return None

    if current_scan is None:
        return None

    parent_id = trade["id"]
    direction = trade["direction"].upper()

    # 1. Leg cap
    leg_count = db.get_leg_count(parent_id)
    if leg_count >= _MAX_ADD_ONS + 1:
        return None

    # 2. Time band
    hours_to_close = current_scan.get("hours_to_close")
    if hours_to_close is None:
        return None

    add_on_index = leg_count  # 0-indexed: leg_count=1 means this is add-on #1
    required_hours = (_MAX_ADD_ONS - add_on_index + 1) * _LEG_SPACING_H
    if hours_to_close >= required_hours:
        return None

    # 3. Cooldown
    latest_leg = db.get_latest_leg(parent_id)
    if latest_leg:
        hours_since = _hours_since(latest_leg.get("opened_at", ""))
        if hours_since is not None and hours_since < _MIN_COOLDOWN_H:
            return None

    # 4. Ensemble ratchet — conviction must be equal or stronger than previous leg
    cur_ens_yes = current_scan.get("ens_yes")
    cur_ens_n   = current_scan.get("ens_n")
    if cur_ens_yes is None or not cur_ens_n:
        return None

    prev_ens_yes = latest_leg.get("entry_ensemble_yes") if latest_leg else None
    prev_ens_n   = latest_leg.get("entry_ensemble_n") if latest_leg else None
    if prev_ens_yes is None or not prev_ens_n:
        return None

    cur_ratio  = cur_ens_yes / cur_ens_n
    prev_ratio = prev_ens_yes / prev_ens_n

    if direction == "NO":
        # Stronger NO = fewer YES votes (lower ratio)
        if cur_ratio > prev_ratio:
            return None
    else:
        # Stronger YES = more YES votes (higher ratio)
        if cur_ratio < prev_ratio:
            return None

    # 5. Kelly sizing
    balance = db.get_balance()
    model_prob = current_scan.get("model_prob", 0)
    market_price = current_scan.get("market_price", 0.5)
    days_to_res = current_scan.get("days_to_resolution", 0)

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
    )
    if size <= 0:
        return None

    # 7. Exposure cap
    open_trades = db.get_open_trades()
    total_exposure = sum(t["size_usdc"] for t in open_trades)
    if balance > 0 and (total_exposure + size) / balance > _MAX_EXPOSURE:
        return None

    new_leg_number = leg_count + 1

    return {
        "size_usdc":        size,
        "direction":        direction,
        "model_prob":       model_prob,
        "market_price":     market_price,
        "ens_yes":          cur_ens_yes,
        "ens_n":            cur_ens_n,
        "ens_pct":          cur_ratio,
        "leg_number":       new_leg_number,
        "parent_trade_id":  parent_id,
        "hours_to_close":   hours_to_close,
        "days_to_resolution": days_to_res,
    }


def _hours_since(iso_str: str) -> float | None:
    """Return hours elapsed since an ISO timestamp, or None if unparseable."""
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600
    except Exception:
        return None
```

- [ ] **Step 2: Commit**

```bash
git add weather_extended.py
git commit -m "feat: add weather_extended.py — extended positions eligibility logic"
```

---

### Task 4: Integrate Extended Pass into Bot Loop

**Files:**
- Modify: `weather_bot.py`

- [ ] **Step 1: Add import**

At the top of `weather_bot.py`, after the existing imports (around line 28), add:

```python
from weather_extended import check_extended_position
```

- [ ] **Step 2: Add `run_extended_positions_pass()` function**

Add this function after `run_entry_pass()` (after line 220) and before `_prompt_startup()`:

```python
# -- Extended positions pass ---------------------------------------------------

def run_extended_positions_pass(dry_run: bool = False):
    """Check open parent trades for scale-in add-on eligibility."""
    if not WEATHER.get("extended_positions_enabled", True):
        return

    open_trades = db.get_open_trades()
    # Only evaluate parent trades (initial entries)
    parents = [t for t in open_trades if not t.get("parent_trade_id")]

    if not parents:
        return

    for trade in parents:
        # Build minimal market dict for layer scan (same pattern as exit pass)
        market_data = {
            "question": trade["market_name"],
            "id":       trade["market_id"],
            "end_date": trade.get("end_date", ""),
            "price":    trade.get("entry_price") or trade.get("fill_price", 0.5),
            "token_id": trade.get("token_id"),
        }

        # Fetch fresh scan data for ensemble + model prob
        try:
            scan_data = _layer.scan(market_data)
        except Exception:
            continue

        if not scan_data or scan_data.get("ensemble_n", 0) < 10:
            continue

        # Build the scan result dict that check_extended_position expects
        ens_yes = scan_data.get("yes_ensemble")
        ens_n   = scan_data.get("ensemble_n")
        model_prob = scan_data.get("prob")
        # market_price = current YES price; use current_price from the latest refresh
        # For NO trades, market_price is still the YES price (Kelly handles direction)
        market_price = trade.get("entry_price") or 0.5

        # Get current market price from the CLOB for accurate Kelly inputs
        try:
            import markets.polymarket as polymarket
            market_info = polymarket.get_market_by_id(trade["market_id"])
            if market_info and market_info.get("price"):
                market_price = market_info["price"]
        except Exception:
            pass

        hours_to_close = None
        end_date = trade.get("end_date", "")
        if end_date:
            try:
                from datetime import datetime, timezone
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                hours_to_close = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                pass

        days_to_res = int(hours_to_close / 24) if hours_to_close is not None else 0

        current_scan = {
            "ens_yes":            ens_yes,
            "ens_n":              ens_n,
            "ens_pct":            ens_yes / ens_n if ens_yes is not None and ens_n else None,
            "model_prob":         model_prob,
            "market_price":       market_price,
            "hours_to_close":     hours_to_close,
            "days_to_resolution": days_to_res,
        }

        result = check_extended_position(trade, current_scan)
        if result is None:
            continue

        city      = (trade.get("city") or "?").title()
        direction = result["direction"]
        leg_num   = result["leg_number"]

        print(
            f"\n  [extend] {city} {direction} — adding leg {leg_num}  "
            f"size=${result['size_usdc']:.2f}  ens={int(result['ens_yes'])}/{result['ens_n']}  "
            f"hours_left={result['hours_to_close']:.1f}h"
        )

        if dry_run:
            print("  [extend] DRY RUN — add-on not placed.")
            continue

        # Build market dict for place_order
        market = {
            "id":          trade["market_id"],
            "question":    trade["market_name"],
            "end_date":    trade.get("end_date"),
            "price":       market_price,
            "token_id":    trade.get("token_id"),
            "no_token_id": trade.get("token_id") if direction == "NO" else None,
            "city":        trade.get("city"),
            "market_url":  trade.get("market_url"),
            "liquidity":   trade.get("liquidity"),
            "volume":      trade.get("volume_24h"),
        }

        estimate = {
            "probability":        result["model_prob"],
            "edge_score":         (result["model_prob"] - market_price) * 100 if direction == "YES"
                                  else (market_price - result["model_prob"]) * 100,
            "sources":            ["weather_forecast"],
            "entry_ensemble_pct": result["ens_pct"],
            "entry_ensemble_yes": result["ens_yes"],
            "entry_ensemble_n":   result["ens_n"],
            "threshold":          trade.get("threshold"),
        }

        _executor.place_extended_order(
            market=market,
            direction=direction,
            size_usdc=result["size_usdc"],
            estimate=estimate,
            parent_trade_id=result["parent_trade_id"],
            leg_number=result["leg_number"],
        )
```

- [ ] **Step 3: Insert the pass into the main loop**

In the `run()` function's main `while True:` loop, add the extended positions pass between the entry pass and the trader monitor. After the entry pass block (after line ~379), add:

```python
        # Extended positions pass — scale into existing positions
        if risk_state == RiskState.NORMAL:
            run_extended_positions_pass(dry_run=dry_run)
```

- [ ] **Step 4: Commit**

```bash
git add weather_bot.py
git commit -m "feat(bot): integrate run_extended_positions_pass into main loop"
```

---

### Task 5: Executor — place_extended_order

**Files:**
- Modify: `executor/paper.py`

- [ ] **Step 1: Add `place_extended_order` method to PaperExecutor**

Add this method after `place_order()` in `executor/paper.py` (after line ~117):

```python
    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position."""
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[paper] Skipping add-on — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        # Select token (same logic as place_order)
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return

        fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", size_usdc)

        if filled_usdc < 1.0:
            print(f"[paper] Skipping add-on — order book too thin")
            return

        # Slippage check
        yes_price = market.get("price", 0.5)
        if direction == "YES":
            token_mid = yes_price
        else:
            token_mid = 1.0 - yes_price

        # Slippage check with size reduction fallback (75%, 50%, 25%)
        max_slippage = WEATHER.get("entry_max_slippage_pct", 0.05)
        slippage_pct = abs(fill_price - token_mid) / token_mid if token_mid > 0 else 0
        if slippage_pct > max_slippage:
            # Try smaller sizes
            for frac in (0.75, 0.50, 0.25):
                reduced = size_usdc * frac
                if reduced < 1.0:
                    break
                fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", reduced)
                slippage_pct = abs(fill_price - token_mid) / token_mid if token_mid > 0 else 0
                if slippage_pct <= max_slippage:
                    break
            else:
                print(f"[paper] Skipping add-on — slippage too high even at 25% size")
                return
            if slippage_pct > max_slippage:
                print(f"[paper] Skipping add-on — slippage {slippage_pct:.1%} > {max_slippage:.0%}")
                return

        shares = filled_usdc / fill_price

        trade = {
            "market_id":       market["id"],
            "market_name":     market["question"],
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.utcnow().isoformat(),
            "hours_to_close_at_entry": _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":       market.get("liquidity"),
            "volume_24h":      market.get("volume"),
            "city":            market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":      market.get("market_url", ""),
            "threshold":       estimate.get("threshold"),
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "parent_trade_id": parent_trade_id,
            "leg_number":      leg_number,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        slippage = abs(fill_price - token_mid)
        print(f"[paper] ADD-ON Leg {leg_number}  {direction:3s}  {market['question'][:50]}")
        print(f"              Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}")
```

- [ ] **Step 2: Add config import**

At the top of `executor/paper.py`, add the WEATHER import:

```python
from config import WEATHER
```

- [ ] **Step 3: Commit**

```bash
git add executor/paper.py
git commit -m "feat(executor): add place_extended_order for add-on legs"
```

---

### Task 6: Exit Logic — Close All Legs Together

**Files:**
- Modify: `weather_bot.py` (exit pass)
- Modify: `executor/paper.py` (add `close_position` helper)

- [ ] **Step 1: Add `close_position` method to PaperExecutor**

Add this method after `close_full()` in `executor/paper.py`:

```python
    def close_position(self, trade: dict, reason: str):
        """
        Close all legs of an extended position (or a single non-extended trade).
        Each leg gets its own close_full with individual P&L.
        """
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            # Fallback: just close the single trade
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)
```

- [ ] **Step 2: Modify `run_exit_pass()` in `weather_bot.py`**

In `run_exit_pass()`, change the exit handling. Replace the block at lines 115-120:

```python
        if sig.should_exit:
            print(
                f"  [exit] {_pos_line}  -- {sig.reason}"
                + (" [URGENT]" if sig.urgent else "")
            )
            _executor.close_full(trade, reason=sig.reason)
```

With:

```python
        if sig.should_exit:
            # For extended positions, close all legs together
            parent_id = trade.get("parent_trade_id") or trade["id"]
            legs = db.get_position_legs(parent_id)
            n_legs = len(legs)
            leg_suffix = f" (closing all {n_legs} legs)" if n_legs > 1 else ""
            print(
                f"  [exit] {_pos_line}  -- {sig.reason}{leg_suffix}"
                + (" [URGENT]" if sig.urgent else "")
            )
            _executor.close_position(trade, reason=sig.reason)
```

- [ ] **Step 3: Skip already-closed legs in exit pass**

The exit pass iterates `db.get_open_trades()`, but after `close_position` closes sibling legs, subsequent iterations may encounter already-closed trades. Add a tracking set at the top of `run_exit_pass()`:

After line 68 (`open_trades = db.get_open_trades()`), add:

```python
    closed_this_pass = set()
```

Then wrap the trade loop body. After `for trade in open_trades:`, add:

```python
        if trade["id"] in closed_this_pass:
            continue
```

And after the `close_position` call, add:

```python
            # Track all legs closed to skip them in remaining iterations
            for leg in legs:
                closed_this_pass.add(leg["id"])
```

- [ ] **Step 4: Commit**

```bash
git add weather_bot.py executor/paper.py
git commit -m "feat(exit): close all legs together on exit trigger"
```

---

### Task 7: Dashboard — Open Positions with Expander

**Files:**
- Modify: `ui/weather_dashboard.py` (open positions section, lines ~347-462)

- [ ] **Step 1: Refactor open positions table to group extended positions**

Replace the open positions section (from `st.subheader(f"Open Positions ({len(open_trades)})")` through the `st.dataframe(df_open, ...)` call and per-trade close buttons) with logic that:

1. Separates parent trades from child legs
2. Shows parent rows with aggregate stats in the main table
3. Adds `st.expander` below each extended position row

The key changes:

a) After fetching `open_trades`, build a parent→legs mapping:

```python
# Group trades: parents with aggregated legs, standalone trades unchanged
parent_map = {}   # parent_id → list of child legs
standalone = []   # trades with no extended position relationship

for t in open_trades:
    pid = t.get("parent_trade_id")
    if pid is not None:
        parent_map.setdefault(pid, []).append(t)
    elif any(c.get("parent_trade_id") == t["id"] for c in open_trades):
        parent_map.setdefault(t["id"], [])  # parent with children
    else:
        standalone.append(t)

# Build display rows: standalone trades + parent aggregates
display_trades = list(standalone)
parent_aggregates = {}  # trade_id → aggregate dict for expanders

for pid, children in parent_map.items():
    parent = next((t for t in open_trades if t["id"] == pid), None)
    if parent is None:
        continue
    all_legs = [parent] + children
    total_size = sum(l["size_usdc"] for l in all_legs)
    total_shares = sum(l.get("shares", 0) for l in all_legs)
    weighted_fill = sum(l["fill_price"] * l["size_usdc"] for l in all_legs) / total_size if total_size else 0
    total_unreal = sum(_unreal_pnl(l) for l in all_legs)
    leg_count = len(all_legs)
    max_legs = WEATHER.get("extended_positions_max_add_ons", 2) + 1

    # Create a synthetic aggregate trade dict for the table row
    agg = dict(parent)
    agg["size_usdc"] = total_size
    agg["shares"] = total_shares
    agg["fill_price"] = weighted_fill
    agg["_unreal_override"] = total_unreal
    agg["_leg_display"] = f"{leg_count}/{max_legs} legs"
    agg["_legs"] = all_legs

    display_trades.append(agg)
    parent_aggregates[pid] = agg
```

b) In the row-building loop, add a "Legs" column for extended positions. For each row, check if `t.get("_leg_display")` exists and include it.

c) After the main `st.dataframe()`, add expanders for each extended position:

```python
for pid, agg in parent_aggregates.items():
    legs = agg["_legs"]
    city = (agg.get("city") or "?").title()
    direction = agg.get("direction", "?").upper()
    with st.expander(f"{city} {direction} — {len(legs)} legs"):
        leg_rows = []
        for leg in sorted(legs, key=lambda l: l.get("leg_number") or 1):
            leg_rows.append({
                "Leg":       leg.get("leg_number") or 1,
                "Fill":      round(leg["fill_price"], 3),
                "Size $":    round(leg["size_usdc"], 2),
                "Shares":    round(leg.get("shares", 0), 2),
                "Ens Entry": f"{int(leg.get('entry_ensemble_yes', 0))}/{leg.get('entry_ensemble_n', 0)}"
                             if leg.get("entry_ensemble_n") else "—",
                "P&L $":     round(_unreal_pnl(leg), 2),
                "Opened":    (leg.get("opened_at") or "")[:16],
            })
        st.dataframe(pd.DataFrame(leg_rows), hide_index=True, width='stretch')
```

d) For per-trade close buttons on extended positions, the confirmation dialog should show the total leg count:

```python
# When building the close button label for extended positions:
if t.get("_legs"):
    n_legs = len(t["_legs"])
    label = f"Close: {city} {direction} {threshold} ({n_legs} legs)"
```

And in the confirmation handler, use `close_position` instead of `close_full`:

```python
executor.close_position(t, reason="manual_close")
```

- [ ] **Step 2: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(dashboard): group extended positions with leg expanders in open positions"
```

---

### Task 8: Dashboard — Closed Positions with Expander

**Files:**
- Modify: `ui/weather_dashboard.py` (trade history section, lines ~535-590)

- [ ] **Step 1: Apply same grouping pattern to closed trades**

Same logic as Task 7 but for the trade history section. Group closed trades by `parent_trade_id`, show aggregate P&L in the main row, and add expanders for individual leg detail.

Key differences from open positions:
- Use `pnl` and `exit_price` instead of unrealized P&L
- Aggregate P&L is sum of individual leg P&Ls
- Weighted average exit_price across legs
- Show exit_reason (same for all legs)

The implementation pattern is identical to Task 7 — build `parent_map`, create aggregate rows, render expanders. Apply the same grouping code adapted for closed trade fields.

For the `get_position_legs` query used here, note that closed legs need a variant that doesn't filter by `status = 'open'`. Add a `get_all_position_legs` function to `db.py`:

```python
def get_all_position_legs(parent_id: int) -> list[dict]:
    """Return all legs of a position regardless of status, ordered by leg_number."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) "
            "ORDER BY leg_number",
            (parent_id, parent_id),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 2: Commit**

```bash
git add ui/weather_dashboard.py db.py
git commit -m "feat(dashboard): group extended positions in trade history with leg expanders"
```

---

### Task 9: Simplify Token ID Handling in Add-Ons

**Files:**
- Modify: `executor/paper.py` (`place_extended_order`)

The parent trade's `token_id` already stores the correct directional token (YES token for YES bets, NO token for NO bets — selected by `place_order` at entry). Add-on legs trade the same direction, so they use the same token.

- [ ] **Step 1: Simplify `place_extended_order` token selection**

In `place_extended_order` (written in Task 5), replace the YES/NO token selection block with:

```python
        # Parent trade already stores the correct directional token_id
        token_id = market.get("token_id")
        if not token_id:
            return
```

Remove the direction-based `if direction == "YES"` / `else` token selection entirely.

- [ ] **Step 2: Ensure `run_extended_positions_pass` passes the parent's token_id**

In the market dict built in Task 4, make sure `token_id` comes from the parent trade:

```python
        market = {
            "id":          trade["market_id"],
            "question":    trade["market_name"],
            "end_date":    trade.get("end_date"),
            "price":       market_price,
            "token_id":    trade.get("token_id"),  # already the correct directional token
            "city":        trade.get("city"),
            "market_url":  trade.get("market_url"),
            "liquidity":   trade.get("liquidity"),
            "volume":      trade.get("volume_24h"),
        }
```

- [ ] **Step 3: Commit**

```bash
git add executor/paper.py weather_bot.py
git commit -m "fix: use parent's stored token_id directly for add-on legs"
```

---

### Task 10: Duplicate Check Bypass for Add-Ons

**Files:**
- Modify: `executor/paper.py`

The existing `place_order` has a duplicate guard at line 42:

```python
if db.get_open_trade_for_market(market["id"]):
    return
```

`place_extended_order` must NOT have this check — the whole point is adding to a market with an existing position. Verify that `place_extended_order` (written in Task 5) does not include this guard. It shouldn't since we wrote it from scratch, but confirm.

Also verify that the scanner's `exclude_market_ids` filter (which excludes markets with open positions) does NOT prevent the extended positions pass from getting scan data. The extended pass uses `_layer.scan()` directly on the market, not `run_scan()`, so it bypasses the scanner filter entirely. No change needed.

- [ ] **Step 1: Verify — no code changes needed**

Read `place_extended_order` and confirm it has no `get_open_trade_for_market` check. Read `run_extended_positions_pass` and confirm it calls `_layer.scan()` directly. Both should already be correct from Tasks 4 and 5.

- [ ] **Step 2: Commit (only if changes were needed)**

---

### Task 11: Settle Resolved — Handle Extended Legs

**Files:**
- Modify: `weather_resolver.py`

- [ ] **Step 1: Verify resolution handles legs correctly**

Read `weather_resolver.py` and check `run_resolve_pass()`. It should iterate `db.get_open_trades()` and call `executor.settle_resolved()` per trade. Since each leg is a separate open trade with the same `market_id` and `end_date`, resolution will naturally settle each leg independently. The P&L per leg is calculated from its own `fill_price` and `shares`.

No code change should be needed — just verify the flow works. Each leg:
- Has its own `market_id` (same as parent)
- Gets matched to the same resolution event
- Gets `settle_resolved()` called individually
- Gets its own `pnl`, `exit_price`, `forecast_correct` written

- [ ] **Step 2: Commit (only if changes were needed)**

---

### Task 12: Final Integration Verification

**Files:** None (manual check)

- [ ] **Step 1: Start the bot with `--dry-run` and verify:**
  - No import errors
  - Extended positions pass runs without errors (prints nothing since no eligible trades yet)
  - Config keys load correctly
  - DB migration adds the new columns

- [ ] **Step 2: Verify dashboard loads without errors:**
  - Open positions table renders (even with no extended positions)
  - Closed positions table renders
  - No Streamlit errors in the console

- [ ] **Step 3: Commit any remaining fixes**

```bash
git add -A
git commit -m "feat: extended positions — complete implementation"
```
