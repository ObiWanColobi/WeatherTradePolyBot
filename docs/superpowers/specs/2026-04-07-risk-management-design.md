# Risk Management System — Design Spec

**Date:** 2026-04-07
**Status:** Draft — awaiting review

---

## 1. Problem Statement

The weather trading bot has no portfolio-level risk controls. The existing safeguards (max positions, max exposure %, per-trade adverse exit) are all entry-gate or per-trade checks. There is no mechanism to:

- Detect when the model may be malfunctioning (sustained losing streak)
- Halt entries automatically when something is wrong
- Manually close positions from the dashboard
- Receive alerts when away from the screen

### Why traditional drawdown logic doesn't apply

Prediction markets resolve binary (0 or 1) on a known schedule. Unrealized P&L mid-hold is CLOB noise, not a signal of terminal value. High-conviction trades that look bad mid-hold still resolve correctly 73% of the time. Position shedding based on drawdown or unrealized P&L would destroy the bot's proven edge.

The real risks are: the model breaks silently, low-quality entries bleed capital, or a bug corrupts decision-making (as seen with the calibration inversion bug). The appropriate response is a circuit breaker — detect abnormal realized losses, halt entries, and notify the operator.

---

## 2. Components

### 2.1 Daily Loss Circuit Breaker

**Purpose:** Detect when realized losses in a single day exceed a configurable threshold, halt all new entries, and notify. This is a "something is broken" alarm, not a drawdown hedge.

**Trigger logic:**

```
today_realized_losses = sum(abs(pnl) for trades closed today where pnl < 0)
account_value = cash + sum(current_price * shares for open trades)
if (today_realized_losses / account_value) >= daily_loss_limit_pct:
    set risk_state = HALTED
```

Only negative P&L trades count toward the limit. Winning trades do not offset losses for this check — the question is "how much have we lost today," not "what's the net." This ensures a day with 3 big wins and 4 big losses still trips the breaker if the losses are large enough.

The threshold is percentage-based so it scales naturally with account size. A $1,000 account and a $10,000 account get proportionally appropriate limits without reconfiguration.

**What "today" means:** UTC calendar day (midnight reset). Matches Polymarket's resolution schedule.

**When it runs:** At the top of every poll iteration, before exit and entry passes. Checked via `weather_risk.check()`.

**What happens when tripped:**
- `risk_state` set to `HALTED`
- All new entries blocked (entry pass skipped entirely)
- Existing positions are NOT touched — they continue to hold-to-resolution
- Exit pass still runs (existing exit logic for ensemble flips, adverse moves, etc.)
- Resolution pass still runs (markets still settle)
- Dashboard banner updates to show HALTED state
- Email notification sent (once, not every poll)

**Reset behavior:**
- **Paper trading:** Auto-resets at UTC midnight. New day, fresh slate. Maximizes data collection. Can also be manually overridden from dashboard (see 2.2.4).
- **Live trading:** Persists until manually overridden from dashboard or bot is restarted. The `risk_state` is held in memory (not DB), so a restart also clears it naturally.
- **Both modes:** When overridden via dashboard, the circuit breaker is suppressed for the remainder of the current UTC day. It cannot re-trip until the next day. This prevents the breaker from immediately re-firing after override (since the same losses still exist in today's data).

**Thresholds:**

| Mode | `daily_loss_limit_pct` | Rationale |
|------|------------------------|-----------|
| Paper (current) | 0.15 (15%) | Very loose — at $1,641 balance, would need ~$246 in losses to trip. Prioritizes data collection over protection. |
| Live (future) | 0.03–0.05 (3–5%) | Tighten based on risk tolerance. Configurable. |

These are starting points. The config key is a single percentage you adjust as comfort level changes.

### 2.2 Manual Trade Controls (Dashboard)

#### 2.2.1 Close All Positions

**Location:** Dashboard sidebar, below exit triggers display, above the existing "Reset Paper Trading" button.

**UX flow:**
1. Red "Close All Positions" button displayed in sidebar
2. Click → button text changes to "Are you sure? This will close ALL open positions. Click again to confirm."
3. Second click within 10 seconds → executes close-all
4. If 10 seconds elapse without second click → reverts to original button state

**Execution:**
- Iterates all open trades via `db.get_open_trades()`
- Calls `executor.close_full(trade, reason="manual_close_all")` for each
- Adds all closed market IDs to the session re-entry block list
- Logs each close with `[risk] manual close-all: {market_name}`
- Sends email notification: "Manual close-all executed. N positions closed."
- Dashboard refreshes to show updated state

**Exit reason:** `"manual_close_all"` — distinct from other exit reasons so it's trackable in trade history and performance analysis.

#### 2.2.2 Per-Trade Close Button

**Location:** New column in the Open Positions table on the dashboard.

**UX flow:**
1. Each open position row gets a "Close" button
2. Click → button text changes to "Confirm close {City} {Direction}?"
3. Second click within 10 seconds → executes close for that single trade
4. If 10 seconds elapse → reverts to original button

**Execution:**
- Calls `executor.close_full(trade, reason="manual_close")` for the specific trade
- Adds closed market ID to the session re-entry block list
- Logs: `[risk] manual close: {market_name}`
- Dashboard refreshes

**Exit reason:** `"manual_close"` — distinct from `"manual_close_all"` for tracking.

#### 2.2.3 Re-Entry Block List

**Purpose:** Prevent the bot from re-entering a market that was manually closed.

**Implementation:**
- File-based: `manually_closed.json` — a JSON list of blocked market_id strings
- Dashboard writes to this file when a manual close (single or close-all) executes
- Bot reads this file each poll in `weather_decision.py` as an additional gate before entry, alongside the existing duplicate market check
- File is deleted on bot startup (fresh session = fresh decisions)
- Also removes a specific market_id from the file when that market resolves (no point blocking a resolved market)

**Why file-based, not DB:** The dashboard and bot are separate processes sharing SQLite. A lightweight JSON file avoids adding session-state columns to the trades table. It's ephemeral by design — deleted on bot restart so stale blocks don't quietly prevent entries long after the original reason is forgotten.

**Why not pure in-memory:** The dashboard (Streamlit) and bot (`weather_bot.py`) run as separate OS processes. An in-memory set in the dashboard wouldn't be visible to the bot. The file acts as a simple IPC mechanism.

#### 2.2.4 Circuit Breaker Override (Dashboard)

**Purpose:** Allow the operator to resume entries from the dashboard after reviewing the situation, without needing to restart the bot process.

**Location:** Appears in the risk status banner (section 2.3) only when state is HALTED.

**UX flow:**
1. When HALTED, the red banner includes an "Override — Resume Entries" button
2. Click → button text changes to "Confirm override? Circuit breaker will be suppressed until tomorrow."
3. Second click → writes override signal, dashboard refreshes, banner clears
4. Circuit breaker is suppressed for the remainder of the current UTC day — it cannot re-trip until the next day, even though today's losses still exceed the threshold

**Implementation:**
- Override signal written to a file (`risk_override.json` with today's UTC date)
- Bot reads this file each poll during `risk_manager.check()` — if override date matches today, skip the loss threshold check
- File is ignored/deleted when the UTC date rolls over (natural expiry)
- Follows the same file-based IPC pattern as the re-entry block list

**Why suppress for the whole day:** The losses that tripped the breaker are still in today's data. Without suppression, the breaker would immediately re-fire on the next poll after override. Suppressing until tomorrow means "I've reviewed the situation and I'm comfortable continuing today."

### 2.3 Dashboard Risk Status Banner

**Location:** Top of the main dashboard area, above the existing metrics row.

**States:**

| State | Banner | Color |
|-------|--------|-------|
| NORMAL | No banner displayed | — |
| HALTED | "ENTRIES HALTED — Daily loss limit reached ({pct}% of account lost today). Override below or wait for midnight reset." | Red |

**Additional info displayed when HALTED:**
- Today's realized losses (count, total $, and % of account)
- Time the circuit breaker tripped
- List of losing trades that contributed
- "Override — Resume Entries" button with double confirmation (see 2.2.4)

### 2.4 Email Notifications

**Purpose:** Alert operator when away from screen. Sent once per event, not every poll.

**Triggers:**
1. Circuit breaker trips → "Daily loss limit reached"
2. Manual close-all executed → "All positions closed manually"
3. Circuit breaker resets (paper midnight) → "Circuit breaker reset — entries resumed"

**Implementation approach:** Lightweight email via SMTP (Python `smtplib` + `email.mime`). Configuration:

```python
# config.py additions
"risk_email_enabled":    False,      # opt-in
"risk_email_smtp_host":  "",         # e.g. "smtp.gmail.com"
"risk_email_smtp_port":  587,
"risk_email_from":       "",
"risk_email_to":         "",
"risk_email_password":   "",         # app password, not account password
```

Email is best-effort — if SMTP fails, log the error and continue. Never let a notification failure block the bot's main loop.

**Email format:** Plain text, short. Subject line contains the event type. Body contains the key numbers (losses today, positions open, current balance).

---

## 3. Integration Points

### 3.1 New file: `weather_risk.py`

The risk module. Contains:
- `RiskState` enum: `NORMAL`, `HALTED`
- `RiskManager` class:
  - `check() -> RiskState` — called every poll, returns current state. Respects override file.
  - `get_today_realized_losses() -> (count, total_usdc)` — queries closed trades for today
  - `get_loss_pct() -> float` — today's losses as percentage of account value
  - `add_manual_close(market_id)` — adds to re-entry block list
  - `is_blocked(market_id) -> bool` — checks re-entry block list
  - `override_for_today()` — writes override file with today's UTC date (called by dashboard)
  - `is_overridden() -> bool` — checks if override file matches today's date
  - `send_alert(event, details)` — sends email if configured
  - `get_status_display() -> dict` — returns data for dashboard banner (state, losses, override status)

### 3.2 Changes to `weather_bot.py`

Main loop modification (pseudocode):

```python
# At top of poll loop, before exit pass:
risk_state = risk_manager.check()

# Exit pass always runs (unchanged)
run_exit_pass()

# Entry pass only if NORMAL
if risk_state == RiskState.NORMAL:
    run_entry_pass()
else:
    print(f"[risk] entries halted — daily loss: ${risk_manager.today_losses:.2f}")

# Resolution pass always runs (unchanged)
run_resolve_pass()
```

### 3.3 Changes to `weather_decision.py`

Add re-entry block check after the existing duplicate market check:

```python
# After line ~142 (existing duplicate check):
if risk_manager.is_blocked(market_id):
    print(f"  [skip] {market_name} — manually closed this session")
    continue
```

### 3.4 Changes to `config.py`

New keys in the WEATHER dict:

```python
# Risk management
"risk_daily_loss_limit_pct":   0.15,    # 15% of account value. paper: loose. tighten for live.
"risk_auto_reset":             True,    # True = paper (midnight reset), False = live (restart to clear)
"risk_email_enabled":          False,
"risk_email_smtp_host":        "",
"risk_email_smtp_port":        587,
"risk_email_from":             "",
"risk_email_to":               "",
"risk_email_password":         "",
```

### 3.5 Changes to `ui/weather_dashboard.py`

1. **Risk banner** — conditional red banner at top of page when HALTED
2. **Close-all button** — sidebar, below exit triggers, with double confirmation
3. **Per-trade close buttons** — new column in Open Positions table, each with double confirmation
4. **Risk status in sidebar** — current state (NORMAL/HALTED), today's realized losses

**Dashboard ↔ bot communication:** The dashboard already shares the same SQLite DB and can import project modules directly (same pattern as the existing "Reset Paper Trading" button, which calls `db.reset_paper_trading()`). Close buttons will import the paper executor and call `close_full()` directly. The re-entry block list must also be persisted to a lightweight file (e.g., `manually_closed.json`) rather than pure in-memory, since the dashboard and bot are separate processes. The bot reads this file each poll; the dashboard writes to it on manual close. File is deleted on bot startup (fresh session).

**Double confirmation pattern:** Follows the existing `st.session_state` two-step pattern used by the Reset button (confirm/cancel columns). Per-trade close buttons use `st.session_state[f"confirm_close_{trade_id}"]` for independent confirmation state per row.

### 3.6 Changes to `db.py`

New query function:

```python
def get_today_realized_losses(self) -> tuple[int, float]:
    """Return (count, total_usdc) of losing trades closed today (UTC)."""
    # SELECT COUNT(*), SUM(ABS(pnl)) FROM trades
    # WHERE status='closed' AND pnl < 0
    # AND DATE(closed_at) = DATE('now')
```

No schema changes needed — all required columns already exist.

---

## 4. What This System Does NOT Do

Explicitly out of scope to keep this focused:

- **No position shedding** — the bot never automatically closes existing positions due to risk state. It only halts new entries.
- **No unrealized P&L monitoring** — mid-hold CLOB prices are noise in prediction markets. Risk is measured on realized outcomes only.
- **No high-water mark tracking** — unnecessary for binary-outcome markets with short hold periods.
- **No VaR calculation** — the positions resolve to 0 or 1; VaR modeling doesn't add value here.
- **No tiered response** — single threshold, single action (halt entries). Simple.

---

## 5. Testing Plan

1. **Circuit breaker trigger:** Manually close trades at a loss (via per-trade close button) until daily limit is hit. Verify entries halt, banner appears, email sends.
2. **Midnight reset (paper):** Trip the breaker, wait for UTC midnight, verify entries resume and banner clears.
3. **Close-all button:** Open 3+ positions, hit close-all with double confirm. Verify all close with `manual_close_all` reason, re-entry block is active, banner/email fire.
4. **Per-trade close:** Close a single position, verify re-entry block for that market ID. Verify the bot skips it on next entry pass.
5. **Re-entry block expiry:** Manually close a trade, then let that market resolve. Verify the block clears for that market_id after resolution.
6. **Email failure resilience:** Set bad SMTP config, trip breaker. Verify bot continues operating and logs the email error without crashing.
7. **Double confirmation timeout:** Click close button once, wait 10+ seconds, verify it reverts without closing.

---

## 6. File Change Summary

| File | Change |
|------|--------|
| `weather_risk.py` | **NEW** — RiskManager class, RiskState enum, email notification, override/block list file management |
| `weather_bot.py` | Add risk check at top of poll loop, pass risk_manager to decision layer |
| `weather_decision.py` | Add re-entry block check |
| `config.py` | Add risk config keys |
| `ui/weather_dashboard.py` | Add risk banner, close-all button, per-trade close buttons, sidebar risk status |
| `db.py` | Add `get_today_realized_losses()` query |
