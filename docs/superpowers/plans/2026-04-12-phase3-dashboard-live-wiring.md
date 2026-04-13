# Phase 3: Dashboard Live Executor Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard's close buttons use the correct executor (Paper or Live) based on `TRADING_MODE`, so closing positions from the UI actually sells shares on the CLOB when in live mode.

**Architecture:** A shared `create_executor()` factory in `executor/__init__.py` replaces all hardcoded `PaperExecutor()` instantiation. The dashboard caches the executor with `@st.cache_resource` to avoid repeated CLOB client initialization. A mode indicator warns users when close buttons will execute real orders.

**Tech Stack:** Python 3.11+, Streamlit, py-clob-client (live mode only)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `executor/__init__.py` | **Modify** | Add `create_executor()` factory function |
| `ui/weather_dashboard.py` | **Modify** | Replace `PaperExecutor()` with cached factory; add mode indicator |
| `weather_bot.py` | **Modify** | Replace local `_create_executor()` with import from `executor` |
| `tests/test_executor_factory.py` | **Create** | Unit tests for `create_executor()` |

---

## Task 1: Executor Factory

**Files:**
- Modify: `executor/__init__.py`
- Create: `tests/test_executor_factory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_executor_factory.py`:

```python
"""Tests for the shared executor factory."""
import pytest
from unittest.mock import patch


def test_create_executor_returns_paper_by_default():
    """Default TRADING_MODE='paper' returns PaperExecutor."""
    with patch("executor.TRADING_MODE", "paper"):
        from executor import create_executor
        from executor.paper import PaperExecutor
        ex = create_executor()
        assert isinstance(ex, PaperExecutor)


def test_create_executor_returns_live_when_configured(monkeypatch):
    """TRADING_MODE='live' returns LiveExecutor (mocked init)."""
    with patch("executor.TRADING_MODE", "live"), \
         patch("executor.live.ClobClient"), \
         patch("executor.live.Claimer"), \
         patch("executor.live.WALLET_PRIVATE_KEY", "0xfakekey"):
        from executor import create_executor
        from executor.live import LiveExecutor
        ex = create_executor()
        assert isinstance(ex, LiveExecutor)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_executor_factory.py -v`
Expected: FAIL — `create_executor` not found in `executor`

- [ ] **Step 3: Implement the factory**

Replace the contents of `executor/__init__.py` with:

```python
def create_executor():
    """
    Create the appropriate executor based on TRADING_MODE config.

    Returns PaperExecutor for paper mode (default), LiveExecutor for live mode.
    Imports are deferred to avoid circular imports.
    """
    from config import TRADING_MODE

    if TRADING_MODE == "live":
        from executor.live import LiveExecutor
        return LiveExecutor()
    else:
        from executor.paper import PaperExecutor
        return PaperExecutor()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_executor_factory.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add executor/__init__.py tests/test_executor_factory.py
git commit -m "feat(executor): add shared create_executor factory"
```

---

## Task 2: Wire Dashboard Close Buttons

**Files:**
- Modify: `ui/weather_dashboard.py:25-27` (imports)
- Modify: `ui/weather_dashboard.py:145-197` (Risk & Controls panel — mode indicator + Close All)
- Modify: `ui/weather_dashboard.py:549-555` (individual close)

- [ ] **Step 1: Update imports**

In `ui/weather_dashboard.py`, change line 25-27 from:

```python
from config import WEATHER, PAPER_STARTING_BALANCE
from weather_risk import RiskManager
from executor.paper import PaperExecutor
```

to:

```python
from config import WEATHER, PAPER_STARTING_BALANCE, TRADING_MODE
from weather_risk import RiskManager
from executor import create_executor
```

- [ ] **Step 2: Add cached executor function**

After the imports block (after line 28), add:

```python
@st.cache_resource
def _get_executor():
    """Cached executor — initializes once per Streamlit session."""
    try:
        return create_executor()
    except Exception as e:
        print(f"[dashboard] Executor init failed: {e}")
        return None
```

- [ ] **Step 3: Add mode indicator to Risk & Controls panel**

In the Risk & Controls panel, after the existing risk status lines (after line 145 `st.caption(f"Loss limit: ...")`), add:

```python
    if TRADING_MODE == "live":
        st.warning("🔴 LIVE mode — closes execute real CLOB orders")
    else:
        st.info("📄 Paper mode")
```

- [ ] **Step 4: Wire Close All button**

Replace the Close All confirmation handler (line 172-178) from:

```python
                if st.button("✅ Confirm Close All", type="primary"):
                    executor = PaperExecutor()
                    closed_ids = []
                    for t in open_trades:
                        executor.close_full(t, reason="manual_close_all")
                        _risk_mgr.add_manual_close(t.get("market_id", ""))
                        closed_ids.append(t.get("market_name", "")[:40])
                        print(f"[risk] manual close-all: {t.get('market_name', '')[:50]}")
```

to:

```python
                if st.button("✅ Confirm Close All", type="primary"):
                    executor = _get_executor()
                    if executor is None:
                        st.error("Cannot close — executor not available. Check wallet config.")
                    else:
                        closed_ids = []
                        for t in open_trades:
                            executor.close_full(t, reason="manual_close_all")
                            _risk_mgr.add_manual_close(t.get("market_id", ""))
                            closed_ids.append(t.get("market_name", "")[:40])
                            print(f"[risk] manual close-all: {t.get('market_name', '')[:50]}")
```

Note: the rest of the block (send_alert, session_state reset, st.success, st.rerun) stays inside the `else` at the same indentation as `closed_ids = []`.

- [ ] **Step 5: Wire individual close button**

Replace the individual close handler (line 550-555) from:

```python
            if st.button("✅ Confirm", key=f"btn_confirm_{trade_id}", type="primary"):
                executor = PaperExecutor()
                if t.get("_legs"):
                    executor.close_position(t, reason="manual_close")
                else:
                    executor.close_full(t, reason="manual_close")
```

to:

```python
            if st.button("✅ Confirm", key=f"btn_confirm_{trade_id}", type="primary"):
                executor = _get_executor()
                if executor is None:
                    st.error("Cannot close — executor not available. Check wallet config.")
                else:
                    if t.get("_legs"):
                        executor.close_position(t, reason="manual_close")
                    else:
                        executor.close_full(t, reason="manual_close")
```

Note: the rest of the block (add_manual_close, print, session_state, st.toast, st.rerun) stays inside the `else`.

- [ ] **Step 6: Verify no remaining PaperExecutor references in dashboard**

Run: `grep -n "PaperExecutor" ui/weather_dashboard.py`
Expected: No matches

- [ ] **Step 7: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(dashboard): wire close buttons to correct executor via TRADING_MODE"
```

---

## Task 3: Deduplicate Bot Executor Creation

**Files:**
- Modify: `weather_bot.py:42-51`

- [ ] **Step 1: Replace local factory with shared import**

In `weather_bot.py`, replace lines 42-51:

```python
def _create_executor():
    if TRADING_MODE == "live":
        from executor.live import LiveExecutor
        return LiveExecutor()
    else:
        from executor.paper import PaperExecutor
        return PaperExecutor()


_executor = _create_executor()
```

with:

```python
from executor import create_executor

_executor = create_executor()
```

- [ ] **Step 2: Verify bot still starts in paper mode**

Run: `python weather_bot.py --dry-run` (Ctrl+C after first poll)
Expected: Bot starts normally in paper mode, no import errors

- [ ] **Step 3: Commit**

```bash
git add weather_bot.py
git commit -m "refactor(bot): use shared create_executor factory"
```
