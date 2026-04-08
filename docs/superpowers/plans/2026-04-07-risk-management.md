# Risk Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily loss circuit breaker, manual trade close controls, and email notifications to the weather trading bot.

**Architecture:** Single new module `weather_risk.py` holds all risk logic (circuit breaker, re-entry block list, override file, email alerts). The bot loop calls `risk_manager.check()` every poll. Dashboard reads risk state and writes manual close / override signals via JSON files for cross-process IPC.

**Tech Stack:** Python 3, SQLite (existing), Streamlit (existing dashboard), smtplib (email)

**Spec:** `docs/superpowers/specs/2026-04-07-risk-management-design.md`

---

### Task 1: Add config keys

**Files:**
- Modify: `config.py:100` (end of WEATHER dict)

- [ ] **Step 1: Add risk management config keys to WEATHER dict**

In `config.py`, add these keys before the closing `}` of the WEATHER dict (before line 100):

```python
    # ── Risk management ──────────────────────────────────────────────────────
    "risk_daily_loss_limit_pct":   0.15,    # 15% of account value — loose for paper, tighten for live
    "risk_auto_reset":             True,    # True = paper (midnight UTC reset), False = live (restart or UI override to clear)
    "risk_email_enabled":          False,   # opt-in email alerts
    "risk_email_smtp_host":        "",      # e.g. "smtp.gmail.com"
    "risk_email_smtp_port":        587,
    "risk_email_from":             "",
    "risk_email_to":               "",
    "risk_email_password":         "",      # app password, not account password
```

- [ ] **Step 2: Verify config loads without errors**

Run: `python -c "from config import WEATHER; print(WEATHER['risk_daily_loss_limit_pct'])"`
Expected: `0.15`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(risk): add risk management config keys"
```

---

### Task 2: Add `get_today_realized_losses()` to db.py

**Files:**
- Modify: `db.py:473` (after `get_stats()`)

- [ ] **Step 1: Add the query function**

After the `get_stats()` function (line 473), add:

```python
def get_today_realized_losses() -> tuple[int, float]:
    """Return (count, total_usdc) of losing trades closed today (UTC).

    Only counts trades with negative P&L. Winning trades do not offset.
    Used by the daily loss circuit breaker in weather_risk.py.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(ABS(pnl)), 0.0) AS total
            FROM trades
            WHERE status = 'closed'
              AND pnl < 0
              AND DATE(closed_at) = DATE('now')
            """
        ).fetchone()
        return int(row["cnt"]), float(row["total"])
```

- [ ] **Step 2: Verify it runs against the existing DB**

Run: `python -c "import db; db.init_db(); print(db.get_today_realized_losses())"`
Expected: `(0, 0.0)` (or actual counts if trades closed today)

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat(risk): add get_today_realized_losses query"
```

---

### Task 3: Create `weather_risk.py` — RiskManager core

**Files:**
- Create: `weather_risk.py`

- [ ] **Step 1: Create the risk module**

```python
"""
weather_risk.py — Portfolio-level risk management for the weather trading bot.

Daily loss circuit breaker: halts new entries when realized losses exceed a
percentage threshold of account value. Does NOT close existing positions.

File-based IPC for dashboard communication:
  - manually_closed.json  — re-entry block list (markets manually closed)
  - risk_override.json    — circuit breaker override signal
"""

import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path

import db
from config import WEATHER

# ── File paths for cross-process IPC ─────────────────────────────────────────
_DATA_DIR = Path(__file__).parent
_BLOCK_LIST_FILE = _DATA_DIR / "manually_closed.json"
_OVERRIDE_FILE   = _DATA_DIR / "risk_override.json"


class RiskState(Enum):
    NORMAL = "NORMAL"
    HALTED = "HALTED"


class RiskManager:
    def __init__(self):
        self._state: RiskState = RiskState.NORMAL
        self._halted_at: str | None = None
        self._alert_sent_today: str | None = None  # UTC date string of last alert
        self._today_losses: float = 0.0
        self._today_loss_count: int = 0

    # ── Startup ──────────────────────────────────────────────────────────────

    def startup_cleanup(self):
        """Delete IPC files from previous session. Call once at bot startup."""
        for f in (_BLOCK_LIST_FILE, _OVERRIDE_FILE):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        print("[risk] Startup cleanup — cleared block list and override files.")

    # ── Circuit breaker ──────────────────────────────────────────────────────

    def check(self) -> RiskState:
        """Check daily loss circuit breaker. Call at the top of every poll.

        Returns RiskState.NORMAL if entries are allowed,
        RiskState.HALTED if daily loss limit has been breached.
        """
        today_utc = datetime.now(timezone.utc).date().isoformat()

        # Auto-reset at midnight (paper mode)
        if WEATHER.get("risk_auto_reset", True):
            if self._halted_at and self._halted_at[:10] != today_utc:
                self._state = RiskState.NORMAL
                self._halted_at = None
                self._alert_sent_today = None
                print("[risk] Midnight reset — circuit breaker cleared.")
                self.send_alert("circuit_breaker_reset",
                                "Circuit breaker auto-reset at midnight. Entries resumed.")

        # Check for dashboard override
        if self._state == RiskState.HALTED and self.is_overridden():
            self._state = RiskState.NORMAL
            self._halted_at = None
            print("[risk] Dashboard override active — circuit breaker suppressed for today.")
            return self._state

        # If already halted today and not overridden, stay halted
        if self._state == RiskState.HALTED:
            return self._state

        # If overridden today, skip the check entirely
        if self.is_overridden():
            return self._state

        # Evaluate daily losses
        self._today_loss_count, self._today_losses = db.get_today_realized_losses()

        if self._today_losses <= 0:
            return self._state

        # Get account value for percentage calculation
        balance = db.get_balance()
        open_trades = db.get_open_trades()
        position_value = sum(
            (t.get("current_price") or t["fill_price"]) * t["shares"]
            for t in open_trades
        )
        account_value = balance + position_value

        if account_value <= 0:
            return self._state

        loss_pct = self._today_losses / account_value
        limit_pct = WEATHER.get("risk_daily_loss_limit_pct", 0.15)

        if loss_pct >= limit_pct:
            self._state = RiskState.HALTED
            self._halted_at = datetime.now(timezone.utc).isoformat()
            print(f"[risk] CIRCUIT BREAKER TRIPPED — "
                  f"daily losses ${self._today_losses:.2f} = {loss_pct:.1%} of account "
                  f"(limit: {limit_pct:.0%}). Entries halted.")

            # Send email alert once per day
            if self._alert_sent_today != today_utc:
                self.send_alert(
                    "circuit_breaker_tripped",
                    f"Daily loss limit reached.\n\n"
                    f"Losses today: {self._today_loss_count} trades, "
                    f"${self._today_losses:.2f} ({loss_pct:.1%} of account)\n"
                    f"Account value: ${account_value:.2f}\n"
                    f"Open positions: {len(open_trades)}\n"
                    f"Cash: ${balance:.2f}",
                )
                self._alert_sent_today = today_utc

        return self._state

    def get_loss_pct(self) -> float:
        """Today's realized losses as a fraction of account value."""
        balance = db.get_balance()
        open_trades = db.get_open_trades()
        position_value = sum(
            (t.get("current_price") or t["fill_price"]) * t["shares"]
            for t in open_trades
        )
        account_value = balance + position_value
        if account_value <= 0:
            return 0.0
        return self._today_losses / account_value

    # ── Re-entry block list ──────────────────────────────────────────────────

    def add_manual_close(self, market_id: str):
        """Add a market_id to the re-entry block list. Called by dashboard."""
        blocked = self._read_block_list()
        if market_id not in blocked:
            blocked.append(market_id)
            self._write_block_list(blocked)

    def is_blocked(self, market_id: str) -> bool:
        """Check if a market_id is on the re-entry block list."""
        return market_id in self._read_block_list()

    def clear_resolved(self, market_id: str):
        """Remove a resolved market from the block list."""
        blocked = self._read_block_list()
        if market_id in blocked:
            blocked.remove(market_id)
            self._write_block_list(blocked)

    def _read_block_list(self) -> list[str]:
        try:
            return json.loads(_BLOCK_LIST_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_block_list(self, blocked: list[str]):
        _BLOCK_LIST_FILE.write_text(json.dumps(blocked))

    # ── Override ─────────────────────────────────────────────────────────────

    def override_for_today(self):
        """Write override signal. Called by dashboard. Suppresses breaker for today."""
        today_utc = datetime.now(timezone.utc).date().isoformat()
        _OVERRIDE_FILE.write_text(json.dumps({"date": today_utc}))
        print(f"[risk] Override written for {today_utc}.")

    def is_overridden(self) -> bool:
        """Check if override file exists and matches today's UTC date."""
        try:
            data = json.loads(_OVERRIDE_FILE.read_text())
            today_utc = datetime.now(timezone.utc).date().isoformat()
            return data.get("date") == today_utc
        except (FileNotFoundError, json.JSONDecodeError):
            return False

    # ── Email alerts ─────────────────────────────────────────────────────────

    def send_alert(self, event: str, body: str):
        """Send email alert. Best-effort — never raises."""
        if not WEATHER.get("risk_email_enabled", False):
            return

        smtp_host = WEATHER.get("risk_email_smtp_host", "")
        smtp_port = WEATHER.get("risk_email_smtp_port", 587)
        from_addr = WEATHER.get("risk_email_from", "")
        to_addr   = WEATHER.get("risk_email_to", "")
        password  = WEATHER.get("risk_email_password", "")

        if not all([smtp_host, from_addr, to_addr, password]):
            print(f"[risk] Email not configured — skipping alert: {event}")
            return

        subject_map = {
            "circuit_breaker_tripped": "WEATHER BOT — Daily Loss Limit Reached",
            "circuit_breaker_reset":   "WEATHER BOT — Circuit Breaker Reset",
            "manual_close_all":        "WEATHER BOT — All Positions Closed Manually",
        }
        subject = subject_map.get(event, f"WEATHER BOT — {event}")

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_addr

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(from_addr, password)
                server.send_message(msg)
            print(f"[risk] Email sent: {subject}")
        except Exception as e:
            print(f"[risk] Email failed (non-fatal): {e}")

    # ── Dashboard display ────────────────────────────────────────────────────

    def get_status_display(self) -> dict:
        """Return data for dashboard risk banner."""
        return {
            "state":        self._state.value,
            "halted_at":    self._halted_at,
            "today_losses": self._today_losses,
            "today_count":  self._today_loss_count,
            "loss_pct":     self.get_loss_pct(),
            "limit_pct":    WEATHER.get("risk_daily_loss_limit_pct", 0.15),
            "overridden":   self.is_overridden(),
        }
```

- [ ] **Step 2: Verify module imports cleanly**

Run: `python -c "from weather_risk import RiskManager, RiskState; rm = RiskManager(); print(rm.check())"`
Expected: `RiskState.NORMAL`

- [ ] **Step 3: Commit**

```bash
git add weather_risk.py
git commit -m "feat(risk): add RiskManager with circuit breaker, block list, override, email"
```

---

### Task 4: Integrate risk check into bot loop

**Files:**
- Modify: `weather_bot.py:281-354` (the `run()` function and main loop)

- [ ] **Step 1: Add import at top of weather_bot.py**

At the top of `weather_bot.py`, with the other imports, add:

```python
from weather_risk import RiskManager, RiskState
```

- [ ] **Step 2: Initialize RiskManager in `run()` and call startup cleanup**

In the `run()` function, after `db.init_db()` (line 282), add:

```python
    risk_manager = RiskManager()
    risk_manager.startup_cleanup()
```

- [ ] **Step 3: Add risk check in the main loop**

In the main loop (after the calibration on_poll and resolve pass, before the exit pass — around line 339), add the risk check and gate the entry pass:

Replace this block (lines 339-345):

```python
        # Exit pass
        run_exit_pass()

        # Entry pass — also returns the full weather condition_id set from the scan
        entered, weather_condition_ids = run_entry_pass(already_traded, max_bet, dry_run=dry_run)
        if entered:
            print(f"\n[bot] Entered {entered} new position(s) this poll.")
```

With:

```python
        # Risk check — portfolio-level circuit breaker
        risk_state = risk_manager.check()

        # Exit pass (always runs — existing per-trade exit logic is independent of risk state)
        run_exit_pass()

        # Entry pass — blocked when circuit breaker is tripped
        if risk_state == RiskState.NORMAL:
            entered, weather_condition_ids = run_entry_pass(already_traded, max_bet, dry_run=dry_run)
            if entered:
                print(f"\n[bot] Entered {entered} new position(s) this poll.")
        else:
            print(f"[risk] Entries halted — daily losses: ${risk_manager._today_losses:.2f} "
                  f"({risk_manager.get_loss_pct():.1%} of account)")
            entered = 0
            weather_condition_ids = set()
```

- [ ] **Step 4: Verify bot starts without errors**

Run: `python weather_bot.py --dry-run` and let it complete one poll, then Ctrl+C.
Expected: Bot starts normally, shows `[risk] Startup cleanup` message, completes poll without errors.

- [ ] **Step 5: Commit**

```bash
git add weather_bot.py
git commit -m "feat(risk): integrate circuit breaker check into bot poll loop"
```

---

### Task 5: Add re-entry block check to decision layer

**Files:**
- Modify: `weather_decision.py:132-142` (after duplicate market check)

- [ ] **Step 1: Add import and accept risk_manager parameter**

At the top of `weather_decision.py`, add:

```python
from weather_risk import RiskManager
```

Find the function signature for the main decision function (the one containing the duplicate check at line 132). Add `risk_manager: RiskManager | None = None` as an optional parameter.

- [ ] **Step 2: Add re-entry block check after the duplicate market check**

After the duplicate check block (line 142, after the `continue`), add:

```python
        # ── 2c. Manual close block — re-entry prevention ────────────────────
        if risk_manager and market_id and risk_manager.is_blocked(market_id):
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=f"manually closed this session ({city} {res_date})",
                direction=direction,
                checks=entry.checks,
            ))
            continue
```

- [ ] **Step 3: Pass risk_manager from weather_bot.py**

In `weather_bot.py`, where the decision function is called inside `run_entry_pass()`, pass the `risk_manager` instance. Find where `run_entry_pass` calls the decision function and add `risk_manager=risk_manager` to the call.

Note: `risk_manager` needs to be accessible in `run_entry_pass`. The cleanest approach is to make it a module-level variable in `weather_bot.py` (similar to how `_executor`, `_layer`, etc. are already module-level), then reference it in `run_entry_pass`.

After the `risk_manager = RiskManager()` line added in Task 4, add a module-level reference:

```python
    global _risk_manager
    _risk_manager = risk_manager
```

And at the module level (near the other module-level variables like `_executor`), add:

```python
_risk_manager: RiskManager | None = None
```

Then in `run_entry_pass`, pass `_risk_manager` to the decision function call.

- [ ] **Step 4: Verify bot starts and the block check doesn't interfere with normal operation**

Run: `python weather_bot.py --dry-run` and let it complete one poll.
Expected: No `manually closed this session` messages (block list is empty on startup).

- [ ] **Step 5: Commit**

```bash
git add weather_decision.py weather_bot.py
git commit -m "feat(risk): add re-entry block check in decision layer"
```

---

### Task 6: Add dashboard sidebar — Close All button and risk status

**Files:**
- Modify: `ui/weather_dashboard.py:105-159` (sidebar section)

- [ ] **Step 1: Add imports at top of dashboard**

At the top of `ui/weather_dashboard.py`, add:

```python
from weather_risk import RiskManager
from executor.paper import PaperExecutor
```

And after the existing data loading section (around line 164), create a risk manager instance for reading state:

```python
_risk_mgr = RiskManager()
```

- [ ] **Step 2: Add risk status display and Close All button in sidebar**

In the sidebar (after the "Exit Triggers" section ending at line 125, before the `st.divider()` at line 126), add:

```python
    st.divider()

    # ── Risk Status ──────────────────────────────────────────────────────────
    st.subheader("Risk Status")
    loss_count, loss_total = db.get_today_realized_losses()
    st.caption(f"Losses today: {loss_count} trades, ${loss_total:.2f}")
    st.caption(f"Loss limit: {WEATHER.get('risk_daily_loss_limit_pct', 0.15):.0%} of account")

    st.divider()

    # ── Close All Positions ──────────────────────────────────────────────────
    st.subheader("Manual Controls")

    if "confirm_close_all" not in st.session_state:
        st.session_state.confirm_close_all = False
    if "close_all_time" not in st.session_state:
        st.session_state.close_all_time = None

    if open_trades:
        if not st.session_state.confirm_close_all:
            if st.button("🔴 Close All Positions", type="secondary"):
                st.session_state.confirm_close_all = True
                st.session_state.close_all_time = time.time()
                st.rerun()
        else:
            elapsed = time.time() - (st.session_state.close_all_time or 0)
            if elapsed > 10:
                st.session_state.confirm_close_all = False
                st.rerun()

            st.warning(f"Close ALL {len(open_trades)} open positions?")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Confirm Close All", type="primary"):
                    executor = PaperExecutor()
                    closed_ids = []
                    for t in open_trades:
                        executor.close_full(t, reason="manual_close_all")
                        _risk_mgr.add_manual_close(t.get("market_id", ""))
                        closed_ids.append(t.get("market_name", "")[:40])
                        print(f"[risk] manual close-all: {t.get('market_name', '')[:50]}")
                    _risk_mgr.send_alert(
                        "manual_close_all",
                        f"All positions closed manually.\n\n"
                        f"Closed {len(closed_ids)} positions:\n" +
                        "\n".join(f"  - {name}" for name in closed_ids),
                    )
                    st.session_state.confirm_close_all = False
                    st.success(f"Closed {len(closed_ids)} positions.")
                    time.sleep(1)
                    st.rerun()
            with c2:
                if st.button("❌ Cancel"):
                    st.session_state.confirm_close_all = False
                    st.rerun()
    else:
        st.caption("No open positions to close.")
```

- [ ] **Step 3: Verify dashboard loads without errors**

Run: `streamlit run ui/weather_dashboard.py`
Expected: Dashboard loads, sidebar shows "Risk Status" section with loss count, "Manual Controls" section with Close All button (if positions exist) or "No open positions" caption.

- [ ] **Step 4: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(risk): add close-all button and risk status to dashboard sidebar"
```

---

### Task 7: Add dashboard — risk banner and circuit breaker override

**Files:**
- Modify: `ui/weather_dashboard.py:179-183` (between header and top metrics)

- [ ] **Step 1: Add risk banner between header and top metrics**

After the `st.title(...)` and `st.caption(...)` lines (around line 182), before the top metrics section, add:

```python
# ── Risk banner ──────────────────────────────────────────────────────────────

_risk_status = _risk_mgr.get_status_display()
if _risk_status["state"] == "HALTED" and not _risk_status["overridden"]:
    with st.container():
        st.error(
            f"**ENTRIES HALTED** — Daily loss limit reached "
            f"({_risk_status['loss_pct']:.1%} of account lost today, "
            f"limit: {_risk_status['limit_pct']:.0%}). "
            f"{_risk_status['today_count']} losing trade(s), "
            f"${_risk_status['today_losses']:.2f} total."
        )
        st.caption(f"Tripped at: {(_risk_status['halted_at'] or '')[:19]} UTC")

        # Override button with double confirmation
        if "confirm_override" not in st.session_state:
            st.session_state.confirm_override = False
        if "override_time" not in st.session_state:
            st.session_state.override_time = None

        if not st.session_state.confirm_override:
            if st.button("Override — Resume Entries"):
                st.session_state.confirm_override = True
                st.session_state.override_time = time.time()
                st.rerun()
        else:
            elapsed = time.time() - (st.session_state.override_time or 0)
            if elapsed > 10:
                st.session_state.confirm_override = False
                st.rerun()

            st.warning("Circuit breaker will be suppressed until tomorrow. Are you sure?")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Confirm Override", type="primary"):
                    _risk_mgr.override_for_today()
                    st.session_state.confirm_override = False
                    st.success("Override active — entries resumed for today.")
                    time.sleep(1)
                    st.rerun()
            with c2:
                if st.button("❌ Cancel Override"):
                    st.session_state.confirm_override = False
                    st.rerun()
```

- [ ] **Step 2: Verify banner displays correctly**

This can only be fully tested when the circuit breaker is tripped. For now, verify the dashboard loads without errors.

Run: `streamlit run ui/weather_dashboard.py`
Expected: No banner shown (state is NORMAL). No errors.

- [ ] **Step 3: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(risk): add risk banner with circuit breaker override to dashboard"
```

---

### Task 8: Add per-trade close buttons to Open Positions table

**Files:**
- Modify: `ui/weather_dashboard.py:239-300` (Open Positions section)

- [ ] **Step 1: Replace the dataframe display with a column-based layout that includes close buttons**

The current open positions section (lines 241-300) uses `st.dataframe()` which doesn't support interactive buttons per row. Replace the display with individual rows using `st.columns()` to add a close button per trade.

Replace the open positions section (from `st.subheader(f"Open Positions...")` through the `st.info("No open positions.")`) with:

```python
st.subheader(f"Open Positions ({len(open_trades)})")

if open_trades:
    # Build the dataframe for display (same as before)
    rows = []
    for t in open_trades:
        h = _hours_left(t.get("end_date", ""))
        unreal = _unreal_pnl(t)
        ens_yes = t.get("entry_ensemble_yes")
        ens_n   = t.get("entry_ensemble_n")
        ens_pct = t.get("entry_ensemble_pct")
        if ens_yes is not None and ens_n:
            ens_str = f"{int(ens_yes)}/{int(ens_n)}"
        elif ens_pct is not None:
            ens_str = f"{ens_pct:.0%}"
        else:
            ens_str = "—"
        url = t.get("market_url") or ""
        cur_yes = t.get("current_ensemble_yes")
        cur_n   = t.get("current_ensemble_n")
        cur_ens_str = f"{int(cur_yes)}/{int(cur_n)}" if cur_yes is not None and cur_n else "—"

        rows.append({
            "City":         (t.get("city") or "—").title(),
            "Threshold":    _parse_threshold(t),
            "Bet":          t["direction"].title(),
            "Tier":         _edge_tier(t.get("edge_score")),
            "Edge %":       round((t.get("edge_score") or 0), 1),
            "Model %":      round((t.get("estimated_prob") or 0) * 100, 1),
            "Mkt %":        round((t.get("entry_price") or 0) * 100, 1),
            "Ens. Entry":   ens_str,
            "Ens. Current": cur_ens_str,
            "Tr Ens":       _tr_ens(t.get("market_id", ""), t.get("direction", "")),
            "Fill":         round(t["fill_price"], 3),
            "Current":      round(t.get("current_price") or t["fill_price"], 3),
            "Unreal. P&L":   round(unreal, 2),
            "Unreal. P&L %": round((unreal / t["size_usdc"]) * 100, 1) if t["size_usdc"] else None,
            "Size $":        round(t["size_usdc"], 2),
            "Vol 24h":      round(t["volume_24h"]) if t.get("volume_24h") else None,
            "Closes":       _fmt_hours(h),
            "Market":       url or t.get("market_name", ""),
            "Opened":       (t.get("opened_at") or "")[:16],
        })

    df_open = pd.DataFrame(rows)
    col_cfg = {
        "Edge %":      st.column_config.NumberColumn(format="%.1f%%"),
        "Model %":     st.column_config.NumberColumn(format="%.1f%%"),
        "Mkt %":       st.column_config.NumberColumn(format="%.1f%%"),
        "Size $":      st.column_config.NumberColumn(format="$%.2f"),
        "Unreal. P&L":   st.column_config.NumberColumn(format="$%.2f"),
        "Unreal. P&L %": st.column_config.NumberColumn(format="%.1f%%"),
        "Vol 24h":       st.column_config.NumberColumn(format="$%d"),
    }
    if any(r["Market"].startswith("http") for r in rows):
        col_cfg["Market"] = st.column_config.LinkColumn(
            "Market", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_open, column_config=col_cfg, width='stretch', hide_index=True)

    # Per-trade close buttons
    st.caption("Manual close:")
    for t in open_trades:
        trade_id = t["id"]
        city = (t.get("city") or "?").title()
        direction = t.get("direction", "?").upper()
        state_key = f"confirm_close_{trade_id}"
        time_key = f"close_time_{trade_id}"

        if state_key not in st.session_state:
            st.session_state[state_key] = False
        if time_key not in st.session_state:
            st.session_state[time_key] = None

        if not st.session_state[state_key]:
            if st.button(f"Close: {city} {direction}", key=f"btn_close_{trade_id}"):
                st.session_state[state_key] = True
                st.session_state[time_key] = time.time()
                st.rerun()
        else:
            elapsed = time.time() - (st.session_state[time_key] or 0)
            if elapsed > 10:
                st.session_state[state_key] = False
                st.rerun()

            c1, c2 = st.columns([1, 1])
            with c1:
                if st.button(f"✅ Confirm close {city} {direction}?", key=f"btn_confirm_{trade_id}", type="primary"):
                    executor = PaperExecutor()
                    executor.close_full(t, reason="manual_close")
                    _risk_mgr.add_manual_close(t.get("market_id", ""))
                    print(f"[risk] manual close: {t.get('market_name', '')[:50]}")
                    st.session_state[state_key] = False
                    st.success(f"Closed {city} {direction}.")
                    time.sleep(1)
                    st.rerun()
            with c2:
                if st.button("❌ Cancel", key=f"btn_cancel_{trade_id}"):
                    st.session_state[state_key] = False
                    st.rerun()
else:
    st.info("No open positions.")
```

- [ ] **Step 2: Verify dashboard displays per-trade close buttons**

Run: `streamlit run ui/weather_dashboard.py`
Expected: Open positions table displays as before. Below it, each open trade has a "Close: {City} {Direction}" button. If no open trades, shows "No open positions."

- [ ] **Step 3: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(risk): add per-trade close buttons with double confirmation"
```

---

### Task 9: Clear resolved markets from block list

**Files:**
- Modify: `weather_bot.py:335-337` (resolve pass section)

- [ ] **Step 1: After the resolve pass, clear resolved market_ids from the block list**

In the main loop, after the resolve pass block (around line 337), the `run_resolve_pass` function returns a count of settled trades. We need to also clear those market_ids from the block list.

Find the resolve pass in `weather_bot.py` and update it. The current code is:

```python
        # Resolve pass — settle expired markets before checking exits/entries
        settled = run_resolve_pass(_executor)
        if settled:
            print(f"\n[bot] Settled {settled} resolved position(s).")
```

The `run_resolve_pass` function needs to return the settled market_ids. Check how it works — if it only returns a count, we need to modify it to also return the market_ids. Alternatively, we can read the block list and check which markets have resolved.

The simpler approach: after settling, read the block list and remove any market_id that is no longer in `db.get_open_market_ids()`:

```python
        # Resolve pass — settle expired markets before checking exits/entries
        settled = run_resolve_pass(_executor)
        if settled:
            print(f"\n[bot] Settled {settled} resolved position(s).")
            # Clean up block list — remove resolved markets
            open_mids = db.get_open_market_ids()
            for mid in _risk_manager._read_block_list():
                if mid not in open_mids:
                    _risk_manager.clear_resolved(mid)
```

- [ ] **Step 2: Verify no errors on resolve pass**

Run: `python weather_bot.py --dry-run` — let it run one poll.
Expected: No errors related to block list cleanup.

- [ ] **Step 3: Commit**

```bash
git add weather_bot.py
git commit -m "feat(risk): clear resolved markets from re-entry block list"
```

---

### Task 10: End-to-end verification

**Files:** None (testing only)

- [ ] **Step 1: Verify bot starts cleanly**

Run: `python weather_bot.py --dry-run`
Expected output includes:
- `[risk] Startup cleanup — cleared block list and override files.`
- Normal poll output
- No risk-related errors

Let it run 1-2 polls, then Ctrl+C.

- [ ] **Step 2: Verify dashboard loads with all risk UI elements**

Run: `streamlit run ui/weather_dashboard.py`
Expected:
- Sidebar shows "Risk Status" section with today's losses
- Sidebar shows "Manual Controls" with Close All button (if positions exist)
- No red banner (circuit breaker not tripped)
- Open Positions table has per-trade close buttons below it (if positions exist)

- [ ] **Step 3: Test double confirmation timeout**

On the dashboard:
1. Click "Close All Positions" (or a per-trade close button)
2. Wait 10+ seconds without clicking confirm
3. Verify button reverts to original state

- [ ] **Step 4: Commit final state**

```bash
git add -A
git commit -m "feat(risk): risk management system complete — circuit breaker, manual controls, email alerts"
```
