# Phase 3: Dashboard Live Executor Wiring

**Date:** 2026-04-12
**Branch:** `live`
**Depends on:** Phase 1 (LiveExecutor), Phase 2 (Claim/Redeem)

---

## Problem

The dashboard's "Close All Positions" button and individual "Close" buttons hardcode `PaperExecutor()`. In live mode, pressing these buttons updates the DB (marks trades closed, credits balance) but **never sells the shares on the CLOB**. The positions remain open on Polymarket while the DB thinks they're closed — a balance/position desync.

Affected locations:
- `ui/weather_dashboard.py` line 173 — Close All Positions
- `ui/weather_dashboard.py` line 551 — Individual position close

## Goal

Make the dashboard's close buttons work correctly in both paper and live mode by using the same executor the bot uses, selected by `TRADING_MODE`.

---

## Approach

### 1. Shared Executor Factory — `executor/__init__.py`

Extract executor creation into a shared factory function. Currently `weather_bot.py` has its own `_create_executor()` and the dashboard hardcodes `PaperExecutor()`. Both should use the same factory.

```python
def create_executor():
    from config import TRADING_MODE
    if TRADING_MODE == "live":
        from executor.live import LiveExecutor
        return LiveExecutor()
    else:
        from executor.paper import PaperExecutor
        return PaperExecutor()
```

Imports are deferred inside the function to avoid circular imports — `LiveExecutor` pulls in `config`, `db`, `chain.claimer`, etc.

### 2. Dashboard Cached Executor

The dashboard is a Streamlit app that re-runs its entire script on every user interaction. LiveExecutor's `__init__` does network I/O (CLOB client init, API cred derivation, allowance check). This must only happen once per session.

```python
@st.cache_resource
def _get_executor():
    try:
        return create_executor()
    except Exception as e:
        return None
```

Both close buttons call `_get_executor()`. If it returns `None` (wallet not configured, RPC down, allowance check failed), the button shows an error message instead of crashing the dashboard.

### 3. Dashboard Mode Indicator

Add a visual badge in the Risk & Controls panel showing the current trading mode, so it's immediately obvious whether close buttons will execute real CLOB orders or paper closes.

- Paper mode: `st.info("📄 Paper mode")`
- Live mode: `st.warning("🔴 LIVE mode — closes will sell on CLOB")`

---

## Detailed Changes

### `executor/__init__.py`

Currently empty. Add `create_executor()` factory function as described above.

### `ui/weather_dashboard.py`

**Import changes:**
- Remove: `from executor.paper import PaperExecutor`
- Add: `from executor import create_executor`
- Add: `from config import TRADING_MODE` (already imports `WEATHER` from config — add `TRADING_MODE` to that line)

**Cached executor** (module level):
```python
@st.cache_resource
def _get_executor():
    try:
        return create_executor()
    except Exception as e:
        st.error(f"Executor init failed: {e}")
        return None
```

**Close All button** (line ~173):
- Replace `executor = PaperExecutor()` with `executor = _get_executor()`
- Guard: if `executor is None`, show `st.error("Cannot close — executor not available")` and skip

**Individual close button** (line ~551):
- Same replacement — `PaperExecutor()` → `_get_executor()`
- Same `None` guard

**Mode indicator** — in the Risk & Controls panel, after the existing risk state display:
```python
if TRADING_MODE == "live":
    st.warning("🔴 LIVE mode — closes execute real CLOB orders")
else:
    st.info("📄 Paper mode")
```

### `weather_bot.py`

Replace the local `_create_executor()` function with an import:
```python
from executor import create_executor
_executor = create_executor()
```

Remove the old `_create_executor()` function (lines 42-48).

---

## Paper Mode Behavior — Unchanged

When `TRADING_MODE` is unset or `"paper"` (default), `create_executor()` returns `PaperExecutor()`. The dashboard behaves exactly as it does today. The factory is just a routing layer — it doesn't change what either executor does.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Paper mode, normal | `PaperExecutor` — DB-only close, same as today |
| Live mode, normal | `LiveExecutor` — sells on CLOB, then updates DB |
| Live mode, no wallet key | `create_executor()` raises → `_get_executor()` returns `None` → close buttons show error |
| Live mode, RPC down | Same as above — LiveExecutor init fails gracefully |
| Live mode, CLOB sell fails | LiveExecutor's existing retry logic (mid-0.01 → $0.01 floor → give up) applies |
| `_get_executor()` cached but stale | Streamlit `cache_resource` persists for the session; restart dashboard to re-init |

---

## Files Changed

| File | Change |
|------|--------|
| `executor/__init__.py` | Add `create_executor()` factory function |
| `ui/weather_dashboard.py` | Replace hardcoded `PaperExecutor()` with cached factory; add mode indicator; add `None` guards |
| `weather_bot.py` | Replace local `_create_executor()` with import from `executor` |

---

## Future Work (Not In Scope)

- **Smarter close retry logic:** Currently `close_full()` uses the same `mid-0.01` → `$0.01 floor` strategy on every attempt with no adaptation. Acceptable at $25 max position sizes. Revisit when bet sizes increase.
- **Nonce management:** Current sequential fetch-at-tx-time is correct for a single-threaded bot. Revisit if concurrent on-chain operations are ever needed.
- **Slippage kill-switch:** FOK orders + per-trade slippage check + daily loss circuit breaker already cover this. No evidence of clustered bad fills to justify additional complexity.
