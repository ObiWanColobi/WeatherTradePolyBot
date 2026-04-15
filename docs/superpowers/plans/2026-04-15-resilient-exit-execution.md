# Resilient Exit Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragile 2-attempt FOK exit execution with GTC orders managed across bot cycles, using book-aware repricing until filled.

**Architecture:** New `get_best_bid()` helper in `markets/polymarket.py`, GTC order type support in `executor/live.py`, new `initiate_exit()` and `manage_pending_exit()` methods on the live executor, a two-phase exit pass in `weather_bot.py`, and DB schema additions for tracking pending exit orders. The exit decision layer (`weather_exit.py`) is untouched — only the execution changes.

**Tech Stack:** Python, SQLite (existing `db.py`), py-clob-client SDK (`OrderType.GTC`), Polymarket CLOB REST API.

**Ordering rationale:** DB schema first (needed by everything), then polymarket helper (no dependencies), then executor methods (depends on both), then bot integration (depends on executor), then config key (trivial). Each task commits independently.

**Spec:** [docs/superpowers/specs/2026-04-15-resilient-exit-execution-design.md](docs/superpowers/specs/2026-04-15-resilient-exit-execution-design.md)

---

## File Map

| File | Changes |
|---|---|
| [db.py](db.py) | Add 3 columns (`exit_order_id`, `exit_order_price`, `exit_order_placed_at`), `get_exit_pending_trades()` helper, update `get_position_legs()` to include `exit_pending` status |
| [markets/polymarket.py](markets/polymarket.py) | Add `get_best_bid(token_id)` helper |
| [executor/base.py](executor/base.py) | Add `initiate_exit()` and `manage_pending_exit()` abstract methods |
| [executor/live.py](executor/live.py) | Add `order_type` param to `_post_order()`, implement `initiate_exit()`, `manage_pending_exit()`, `_settle_exit()` |
| [executor/paper.py](executor/paper.py) | Stub `initiate_exit()` (delegates to existing `close_position()`) and `manage_pending_exit()` (no-op) |
| [weather_bot.py](weather_bot.py) | Two-phase exit pass: Phase 1 manages pending exits, Phase 2 evaluates new signals |
| [config.py](config.py) | Add `exit_reprice_min_step` key |

---

### Task 1: DB schema — add exit order tracking columns

**Files:**
- Modify: [db.py:221](db.py#L221) (add 3 new `_safe_add_column` calls)
- Modify: [db.py:343-348](db.py#L343-L348) (new `get_exit_pending_trades()` function)
- Modify: [db.py:471-479](db.py#L471-L479) (update `get_position_legs()` to include `exit_pending`)

- [ ] **Step 1: Add the three new columns in `init_db()`**

In [db.py](db.py), find line 221:
```python
        _safe_add_column(conn, "trades", "claim_last_attempt", "TEXT")              # ISO timestamp of last attempt
```

Insert immediately after:
```python
        _safe_add_column(conn, "trades", "claim_last_attempt", "TEXT")              # ISO timestamp of last attempt

        # Resilient exit execution (2026-04-15) — GTC order tracking
        _safe_add_column(conn, "trades", "exit_order_id",        "TEXT")
        _safe_add_column(conn, "trades", "exit_order_price",     "REAL")
        _safe_add_column(conn, "trades", "exit_order_placed_at", "TEXT")
```

- [ ] **Step 2: Add `get_exit_pending_trades()` function**

In [db.py](db.py), find lines 343-348:
```python
def get_open_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
```

Insert immediately after:
```python
def get_open_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_exit_pending_trades() -> list[dict]:
    """Return parent trades that have a pending GTC exit order on the CLOB."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'exit_pending' "
            "AND exit_order_id IS NOT NULL ORDER BY opened_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 3: Update `get_position_legs()` to include `exit_pending` legs**

In [db.py](db.py), find lines 471-479:
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
```

Replace with:
```python
def get_position_legs(parent_id: int) -> list[dict]:
    """Return all legs of an extended position (parent + children), ordered by leg_number."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) "
            "AND status IN ('open', 'exit_pending') "
            "ORDER BY leg_number",
            (parent_id, parent_id),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Sanity-check the DB initializes cleanly**

Run:
```bash
python -c "import db; db.init_db(); print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Verify the new function works**

Run:
```bash
python -c "import db; db.init_db(); print('pending:', db.get_exit_pending_trades())"
```
Expected: `pending: []`

- [ ] **Step 6: Commit**

```bash
git add db.py
git commit -m "db: add exit order tracking columns and get_exit_pending_trades()"
```

---

### Task 2: Add `get_best_bid()` to polymarket helper

**Files:**
- Modify: [markets/polymarket.py:155](markets/polymarket.py#L155) (insert new function after `get_orderbook()`)

- [ ] **Step 1: Add the `get_best_bid()` function**

In [markets/polymarket.py](markets/polymarket.py), find lines 153-155:
```python
        print(f"[polymarket] Failed to fetch orderbook for {token_id}: {e}")
        return {"bids": [], "asks": []}
```

Insert immediately after:
```python
        print(f"[polymarket] Failed to fetch orderbook for {token_id}: {e}")
        return {"bids": [], "asks": []}


def get_best_bid(token_id: str) -> float | None:
    """Return the highest bid price on the order book, or None if no bids."""
    book = get_orderbook(token_id)
    bids = book.get("bids", [])
    if not bids:
        return None
    try:
        prices = [float(b["price"]) for b in bids if b.get("price")]
        return max(prices) if prices else None
    except (ValueError, KeyError):
        return None
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "from markets.polymarket import get_best_bid; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add markets/polymarket.py
git commit -m "feat: add get_best_bid() helper for exit repricing"
```

---

### Task 3: Add abstract methods to `BaseExecutor`

**Files:**
- Modify: [executor/base.py:24-25](executor/base.py#L24-L25) (insert new abstract methods)

- [ ] **Step 1: Add `initiate_exit()` and `manage_pending_exit()` to the base class**

In [executor/base.py](executor/base.py), find lines 24-25:
```python
    @abstractmethod
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""
```

Insert immediately after:
```python
    @abstractmethod
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""

    @abstractmethod
    def initiate_exit(self, trade: dict, reason: str):
        """Post a GTC sell order to begin exiting a position across bot cycles."""

    @abstractmethod
    def manage_pending_exit(self, trade: dict):
        """Check fill status of a pending GTC exit order, reprice if needed, settle if filled."""
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "from executor.base import BaseExecutor; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add executor/base.py
git commit -m "executor: add initiate_exit and manage_pending_exit abstract methods"
```

---

### Task 4: Add GTC support to `_post_order()`

**Files:**
- Modify: [executor/live.py:120-187](executor/live.py#L120-L187) (add `order_type` parameter to `_post_order()`)

- [ ] **Step 1: Add `order_type` parameter and use it for SELL orders**

In [executor/live.py](executor/live.py), find lines 120-121:
```python
    def _post_order(self, token_id: str, price: float, size: float,
                    side: str) -> dict | None:
```

Replace with:
```python
    def _post_order(self, token_id: str, price: float, size: float,
                    side: str, order_type: str = "FOK") -> dict | None:
```

Then find lines 171-182 (the SELL branch's `_do_post` function):
```python
            def _do_post():
                try:
                    signed = self._client.create_order(order_args)
                    response = self._client.post_order(signed, OrderType.FOK)
                    print(f"[live] Order posted: {response.get('orderID', '?')[:12]}  "
                          f"status={response.get('status', '?')}")
                    return response
                except Exception as e:
                    if "fully filled" in str(e).lower():
                        print(f"[live] FOK rejected — insufficient liquidity")
                        return _FOK_REJECTED
                    raise
```

Replace with:
```python
            def _do_post():
                try:
                    signed = self._client.create_order(order_args)
                    ot = OrderType.GTC if order_type == "GTC" else OrderType.FOK
                    response = self._client.post_order(signed, ot)
                    print(f"[live] Order posted ({order_type}): {response.get('orderID', '?')[:12]}  "
                          f"status={response.get('status', '?')}")
                    return response
                except Exception as e:
                    if "fully filled" in str(e).lower():
                        print(f"[live] {order_type} rejected — insufficient liquidity")
                        return _FOK_REJECTED
                    raise
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import executor.live; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Verify existing FOK behavior is unchanged**

The default `order_type="FOK"` means all existing callers (entries, current closes) are unaffected. Confirm by checking no callers pass the new param yet:

Run:
```bash
grep -n "_post_order" executor/live.py | head -20
```
Expected: all existing calls use positional args only (no `order_type` kwarg).

- [ ] **Step 4: Commit**

```bash
git add executor/live.py
git commit -m "executor: add order_type param to _post_order (GTC support)"
```

---

### Task 5: Implement `initiate_exit()` on live executor

**Files:**
- Modify: [executor/live.py:1014](executor/live.py#L1014) (insert new method after `close_position()`)

- [ ] **Step 1: Add the `initiate_exit()` method**

In [executor/live.py](executor/live.py), find lines 1006-1014:
```python
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)
```

Insert immediately after:
```python
    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)

    def initiate_exit(self, trade: dict, reason: str):
        """Post a GTC sell to begin exiting a position. Tracks order across cycles."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            legs = [trade]

        token_id = trade.get("token_id")
        if not token_id:
            print(f"[live] Cannot initiate exit — no token_id for {trade['market_name'][:50]}")
            return

        total_shares = sum(leg.get("shares", 0) for leg in legs)
        if total_shares <= 0:
            return

        best_bid = polymarket.get_best_bid(token_id)
        if best_bid is not None and best_bid > 0.01:
            sell_price = round(max(best_bid - 0.01, 0.01), 2)
        else:
            sell_price = 0.01

        order_response = self._post_order(token_id, sell_price, total_shares, "SELL", order_type="GTC")
        if order_response is None:
            print(f"[live] GTC sell failed for {trade['market_name'][:50]} — will retry next cycle")
            return

        order_id = order_response.get("orderID", "")
        now_iso = datetime.now(timezone.utc).isoformat()

        parent_trade = next((l for l in legs if l["id"] == parent_id), legs[0])
        db.update_trade(parent_trade["id"], {
            "exit_order_id":        order_id,
            "exit_order_price":     sell_price,
            "exit_order_placed_at": now_iso,
        })

        for leg in legs:
            db.update_trade(leg["id"], {
                "status":      "exit_pending",
                "exit_reason": _append_reason(leg.get("exit_reason"), reason),
            })

        print(f"[live] EXIT INITIATED {trade['market_name'][:55]}")
        print(f"             GTC sell: {total_shares:.2f} shares @ ${sell_price:.2f}  "
              f"order={order_id[:12]}  reason={reason}")
```

- [ ] **Step 2: Add polymarket import if not already present**

Check the imports at the top of [executor/live.py](executor/live.py). The file already imports `from markets import polymarket`, so `polymarket.get_best_bid()` is available. Verify:

Run:
```bash
grep "from markets import polymarket" executor/live.py
```
Expected: one match.

- [ ] **Step 3: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import executor.live; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add executor/live.py
git commit -m "feat: implement initiate_exit() — GTC sell with order tracking"
```

---

### Task 6: Implement `manage_pending_exit()` and `_settle_exit()` on live executor

**Files:**
- Modify: [executor/live.py](executor/live.py) (insert after `initiate_exit()`)

- [ ] **Step 1: Add the `_settle_exit()` helper method**

Insert immediately after the `initiate_exit()` method added in Task 5:

```python
    def _settle_exit(self, parent_trade: dict, legs: list[dict],
                     fill_price: float, filled_shares: float, fee: float):
        """Settle a filled GTC exit order across all legs proportionally."""
        total_shares = sum(leg.get("shares", 0) for leg in legs)
        if total_shares <= 0:
            return

        total_proceeds = filled_shares * fill_price
        now_iso = datetime.now(timezone.utc).isoformat()

        for leg in legs:
            leg_share_frac = leg.get("shares", 0) / total_shares
            leg_proceeds = total_proceeds * leg_share_frac
            leg_fee = fee * leg_share_frac
            leg_cost = leg["size_usdc"]
            leg_pnl = leg_proceeds - leg_cost - leg_fee
            leg_pnl_pct = (leg_pnl / leg_cost * 100) if leg_cost > 0 else 0.0

            db.update_trade(leg["id"], {
                "exit_price":             fill_price,
                "closed_at":              now_iso,
                "status":                 "closed",
                "pnl":                    leg_pnl,
                "pnl_pct":                leg_pnl_pct,
                "hours_to_close_at_exit": _hours_until(leg.get("end_date")),
                "fee_usdc":               (leg.get("fee_usdc") or 0) + leg_fee,
                "exit_order_id":          None,
                "exit_order_price":       None,
                "exit_order_placed_at":   None,
            })

        db.update_balance(total_proceeds)
        db.record_account_value()

        reason = parent_trade.get("exit_reason", "exit")
        name = parent_trade.get("market_name", "unknown")[:55]
        total_cost = sum(leg["size_usdc"] for leg in legs)
        total_pnl = total_proceeds - total_cost - fee

        print(f"[live] EXIT FILLED {name}")
        print(f"             {filled_shares:.2f} shares @ ${fill_price:.3f}  "
              f"P&L: ${total_pnl:+.2f}  Fee: ${fee:.2f}")

        notify("info", "Position Closed",
               f"Exited {name} — {reason}",
               fields={"P&L": f"${total_pnl:+.2f}",
                        "Reason": reason},
               color=COLOR_GREEN)
```

- [ ] **Step 2: Add the `manage_pending_exit()` method**

Insert immediately after `_settle_exit()`:

```python
    def manage_pending_exit(self, trade: dict):
        """Check fill status of a pending GTC exit, reprice if needed, settle if filled."""
        order_id = trade.get("exit_order_id")
        token_id = trade.get("token_id")
        parent_id = trade.get("parent_trade_id") or trade["id"]

        if not order_id or not token_id:
            return

        legs = db.get_position_legs(parent_id)
        if not legs:
            legs = [trade]

        # Check order status on CLOB
        try:
            self._throttle_clob()
            order = self._client.get_order(order_id)
        except Exception as e:
            print(f"[live] Exit order check failed for {order_id[:12]}: {e}")
            return

        if order is None:
            print(f"[live] Exit order {order_id[:12]} not found — will retry next cycle")
            return

        status = (order.get("status") or "").lower()

        # ── Filled ────────────────────────────────────────────────────────────
        if status in ("matched", "filled"):
            trades = order.get("associate_trades") or []
            if trades and isinstance(trades[0], dict):
                total_cost = 0.0
                total_shares = 0.0
                total_fee = 0.0
                for t in trades:
                    p = float(t.get("price", 0))
                    s = float(t.get("size", 0))
                    f = float(t.get("fee", 0))
                    total_cost += p * s
                    total_shares += s
                    total_fee += f
                fill_price = total_cost / total_shares if total_shares > 0 else trade.get("exit_order_price", 0)
            else:
                matched = float(order.get("size_matched", 0))
                fill_price = float(order.get("price", trade.get("exit_order_price", 0)))
                total_shares = matched
                total_fee = 0.0

            if total_shares > 0:
                self._settle_exit(trade, legs, fill_price, total_shares, total_fee)
                return

        # ── Cancelled/expired (e.g. filled between our cancel and repost) ─────
        if status in ("cancelled", "expired", "dead", "canceled"):
            # Check if it was actually filled via trade history before giving up
            result = self._lookup_sell_fills(token_id, trade)
            if result and result[0] is not None and result[1] is not None and result[1] > 0:
                self._settle_exit(trade, legs, result[0], result[1], result[2])
                return
            # Truly cancelled — clear tracking, revert to open so next cycle retriggers exit
            print(f"[live] Exit order {order_id[:12]} was cancelled — reverting to open")
            for leg in legs:
                db.update_trade(leg["id"], {"status": "open"})
            db.update_trade(trade["id"], {
                "exit_order_id": None,
                "exit_order_price": None,
                "exit_order_placed_at": None,
            })
            return

        # ── Partial fill ──────────────────────────────────────────────────────
        size_matched = float(order.get("size_matched", 0))
        orig_size = float(order.get("original_size", 0) or order.get("size", 0) or 0)
        if size_matched > 0 and orig_size > 0 and size_matched < orig_size * 0.99:
            # Partial fill — settle what we got, cancel remainder, repost
            trades_data = order.get("associate_trades") or []
            if trades_data and isinstance(trades_data[0], dict):
                tc = sum(float(t.get("price", 0)) * float(t.get("size", 0)) for t in trades_data)
                ts = sum(float(t.get("size", 0)) for t in trades_data)
                tf = sum(float(t.get("fee", 0)) for t in trades_data)
                fp = tc / ts if ts > 0 else trade.get("exit_order_price", 0)
            else:
                fp = float(order.get("price", trade.get("exit_order_price", 0)))
                ts = size_matched
                tf = 0.0

            # Settle the filled portion (reduces shares on each leg proportionally)
            total_leg_shares = sum(leg.get("shares", 0) for leg in legs)
            if total_leg_shares > 0 and ts > 0:
                fill_frac = ts / total_leg_shares
                # Reduce each leg's shares
                for leg in legs:
                    filled_leg = leg["shares"] * fill_frac
                    remaining = leg["shares"] - filled_leg
                    leg_proceeds = filled_leg * fp
                    leg_fee = tf * (leg["shares"] / total_leg_shares)
                    leg_pnl = leg_proceeds - (filled_leg * leg["fill_price"]) - leg_fee
                    db.update_balance(leg_proceeds)
                    db.update_trade(leg["id"], {
                        "shares": remaining,
                        "size_usdc": remaining * leg["fill_price"],
                        "fee_usdc": (leg.get("fee_usdc") or 0) + leg_fee,
                    })

            # Cancel remainder and repost
            self._cancel_order(order_id)
            unfilled = orig_size - size_matched
            best_bid = polymarket.get_best_bid(token_id)
            reprice = round(max((best_bid or 0.01) - 0.01, 0.01), 2)
            new_resp = self._post_order(token_id, reprice, unfilled, "SELL", order_type="GTC")
            if new_resp:
                db.update_trade(trade["id"], {
                    "exit_order_id": new_resp.get("orderID", ""),
                    "exit_order_price": reprice,
                    "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                print(f"[live] Partial repost failed — will retry next cycle")
            return

        # ── Still open / delayed — reprice if needed ──────────────────────────
        if status in ("live", "open", "delayed", ""):
            best_bid = polymarket.get_best_bid(token_id)
            current_price = trade.get("exit_order_price", 0)
            reprice_step = WEATHER.get("exit_reprice_min_step", 0.01)

            if best_bid is None:
                # No bids at all — drop to floor
                if current_price > 0.01:
                    self._cancel_order(order_id)
                    new_resp = self._post_order(token_id, 0.01, orig_size or sum(l.get("shares", 0) for l in legs), "SELL", order_type="GTC")
                    if new_resp:
                        db.update_trade(trade["id"], {
                            "exit_order_id": new_resp.get("orderID", ""),
                            "exit_order_price": 0.01,
                            "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                        })
                return

            target_price = round(max(best_bid - 0.01, 0.01), 2)

            if current_price > target_price and (current_price - target_price) >= reprice_step:
                self._cancel_order(order_id)
                total_shares = orig_size or sum(l.get("shares", 0) for l in legs)
                new_resp = self._post_order(token_id, target_price, total_shares, "SELL", order_type="GTC")
                if new_resp:
                    db.update_trade(trade["id"], {
                        "exit_order_id": new_resp.get("orderID", ""),
                        "exit_order_price": target_price,
                        "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    print(f"[live] Exit repriced: ${current_price:.2f} -> ${target_price:.2f}  "
                          f"best_bid=${best_bid:.2f}  {trade['market_name'][:40]}")
                else:
                    # Cancel succeeded but repost failed — revert status so next cycle retries
                    print(f"[live] Exit reprice repost failed — will retry next cycle")
            else:
                print(f"[live] Exit order competitive @ ${current_price:.2f}  "
                      f"best_bid=${best_bid:.2f}  {trade['market_name'][:40]}")
```

- [ ] **Step 3: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import executor.live; print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add executor/live.py
git commit -m "feat: implement manage_pending_exit() and _settle_exit()"
```

---

### Task 7: Paper executor stubs

**Files:**
- Modify: [executor/paper.py](executor/paper.py) (add stub methods)

- [ ] **Step 1: Find the `close_position` method in paper.py and add stubs after it**

First, find where `close_position` is in the paper executor:

Run:
```bash
grep -n "def close_position\|def settle_resolved" executor/paper.py
```

Then add the following two methods between `close_position()` and `settle_resolved()`:

```python
    def initiate_exit(self, trade: dict, reason: str):
        """Paper mode: no GTC orders — close immediately."""
        self.close_position(trade, reason=reason)

    def manage_pending_exit(self, trade: dict):
        """Paper mode: no pending exits — no-op."""
        pass
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import executor.paper; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add executor/paper.py
git commit -m "executor: add paper mode stubs for initiate_exit and manage_pending_exit"
```

---

### Task 8: Two-phase exit pass in `weather_bot.py`

**Files:**
- Modify: [weather_bot.py:67-148](weather_bot.py#L67-L148) (restructure `run_exit_pass()`)

- [ ] **Step 1: Add Phase 1 — manage pending exit orders**

In [weather_bot.py](weather_bot.py), find lines 67-76:
```python
def run_exit_pass():
    if not db.get_open_trades():
        return

    _executor.update_open_positions()
    db.record_account_value()

    # Re-fetch after update so the exit loop sees the freshly-written prices,
    # not the stale values from before update_open_positions() ran.
    open_trades = db.get_open_trades()
```

Replace with:
```python
def run_exit_pass():
    # ── Phase 1: Manage pending GTC exit orders ──────────────────────────────
    pending_exits = db.get_exit_pending_trades()
    exit_pending_parents = set()
    for ptrade in pending_exits:
        parent_id = ptrade.get("parent_trade_id") or ptrade["id"]
        if parent_id in exit_pending_parents:
            continue
        exit_pending_parents.add(parent_id)
        _executor.manage_pending_exit(ptrade)

    # ── Phase 2: Evaluate new exit signals ───────────────────────────────────
    if not db.get_open_trades():
        return

    _executor.update_open_positions()
    db.record_account_value()

    # Re-fetch after update so the exit loop sees the freshly-written prices,
    # not the stale values from before update_open_positions() ran.
    open_trades = db.get_open_trades()
```

- [ ] **Step 2: Route exit signals through `initiate_exit()` instead of `close_position()`**

In [weather_bot.py](weather_bot.py), find line 144:
```python
            _executor.close_position(trade, reason=sig.reason)
```

Replace with:
```python
            _executor.initiate_exit(trade, reason=sig.reason)
```

- [ ] **Step 3: Skip `exit_pending` trades in Phase 2**

In [weather_bot.py](weather_bot.py), find lines 80-81 (inside the Phase 2 loop):
```python
        if trade["id"] in closed_this_pass:
            continue
```

Replace with:
```python
        if trade["id"] in closed_this_pass:
            continue
        # Skip trades already being exited via GTC
        parent_id = trade.get("parent_trade_id") or trade["id"]
        if parent_id in exit_pending_parents:
            continue
```

- [ ] **Step 4: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_bot; print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add weather_bot.py
git commit -m "feat: two-phase exit pass with GTC order management"
```

---

### Task 9: Add config key

**Files:**
- Modify: [config.py:118](config.py#L118) (insert new key after late-game exit block)

- [ ] **Step 1: Add the `exit_reprice_min_step` key**

In [config.py](config.py), find line 118:
```python
    "exit_late_game_ensemble_threshold": 0.70,  # ...AND ensemble still shows >= this conviction for us
```

Insert immediately after:
```python
    "exit_late_game_ensemble_threshold": 0.70,  # ...AND ensemble still shows >= this conviction for us

    # ── Resilient exit execution (2026-04-15) ────────────────────────────────
    "exit_reprice_min_step":  0.01,   # minimum price drop per reprice cycle (GTC exit orders)
```

- [ ] **Step 2: Sanity check**

Run:
```bash
python -c "from config import WEATHER; print(WEATHER['exit_reprice_min_step'])"
```
Expected: `0.01`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add exit_reprice_min_step for GTC exit orders"
```

---

### Task 10: Integration verification

**Files:** none modified — runs existing test suite + smoke tests.

- [ ] **Step 1: Run the existing test suite**

Run:
```bash
python -m pytest tests/ -q
```
Expected: same pass/fail baseline as before (74 passed, 4 pre-existing failures). No regressions.

- [ ] **Step 2: Full-app import smoke test**

Run:
```bash
python -c "import weather_bot; import weather_decision; import weather_entry; import weather_extended; import weather_sizing; import executor.weather_exit; import executor.live; import executor.paper; print('all modules ok')"
```
Expected: `all modules ok`

- [ ] **Step 3: Verify DB schema has new columns**

Run:
```bash
python -c "
import db; db.init_db()
from db import get_conn
conn = get_conn()
cols = [row[1] for row in conn.execute('PRAGMA table_info(trades)').fetchall()]
for c in ['exit_order_id', 'exit_order_price', 'exit_order_placed_at']:
    print(f'{c}: {\"present\" if c in cols else \"MISSING\"}')"
```
Expected: all three report `present`.

- [ ] **Step 4: Verify get_position_legs includes exit_pending status**

Run:
```bash
python -c "
import db; db.init_db()
# The SQL should now include 'exit_pending' — verify by checking function source
import inspect
src = inspect.getsource(db.get_position_legs)
print('exit_pending in query:', 'exit_pending' in src)
"
```
Expected: `exit_pending in query: True`

---

## Post-Implementation

After all tasks complete:

1. Update `tasks/todo.md` with a review section noting what was implemented.
2. Deploy to Kamatera live bot only after confirming the GTC order flow works in paper mode first.
3. Monitor the first few live exit cycles to ensure repricing and settlement work correctly.
