# Phase 4 — Monitoring & Alerts for 24/7 Operation

**Date:** 2026-04-12
**Status:** Design
**Branch:** `live` (CLOB-specific pieces); `main` also benefits from health/notifications/API monitoring

## Goal

Make the bot observable and self-reporting for unattended 24/7 operation. Surface critical events, trade activity, and health status through Discord alerts and the dashboard — so the operator knows what's happening without checking logs.

## Constraints

- No impact to paper trading. All new features are additive; CLOB-specific pieces only activate when `TRADING_MODE=live`.
- Discord webhooks are optional — if not configured, notifications silently skip (same pattern as existing email alerts).
- No new threads or async — everything runs in the existing single-threaded poll loop.
- PythonAnywhere compatible (no websockets, no long-running background processes beyond the always-on task).

## Architecture

Three new modules + dashboard additions + bot loop integration:

```
notifications.py    — Discord webhook client + DB persistence
health.py           — Heartbeat writer + crash detection
api_monitor.py      — Shared circuit breaker for all external APIs
dashboard.py        — Notification feed panel + bot-down banner (additions)
weather_bot.py      — Wiring: digest trigger, heartbeat write, breaker integration
```

---

## 1. Notifications (`notifications.py`)

### Discord Webhooks

Two webhook URLs configured in `config.py`:

| Config Key | Channel | Purpose |
|---|---|---|
| `discord_webhook_alerts` | #bot-alerts | Critical + warning events |
| `discord_webhook_trades` | #bot-trades | Trade activity + daily digest |

If a webhook URL is empty, sends to that channel are silently skipped.

### Event Routing

| Event | Channel | Severity |
|---|---|---|
| Circuit breaker tripped | alerts | critical |
| Crash detected on restart | alerts | critical |
| CLOB auth/signing failure | alerts | critical |
| API circuit breaker tripped | alerts | warning |
| Gas too low to claim | alerts | warning |
| Open-Meteo 429 hit | alerts | warning |
| Order filled (entry) | trades | info |
| Position closed (exit) | trades | info |
| Claim completed | trades | info |
| Daily P&L digest | trades | info |

### Discord Message Format

Use Discord embeds with color-coded sidebars:
- **Red** (`0xFF0000`) — critical
- **Yellow** (`0xFFAA00`) — warning
- **Green** (`0x00FF00`) — trade fills / positive P&L
- **Blue** (`0x0066FF`) — digest / informational

Each embed includes: title, description, timestamp, and relevant fields (market name, amount, price, etc. as applicable).

### DB Persistence

Every notification is written to a `notifications` table:

```sql
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,       -- ISO-8601 UTC
    severity    TEXT NOT NULL,       -- critical, warning, info
    channel     TEXT NOT NULL,       -- alerts, trades
    title       TEXT NOT NULL,
    message     TEXT NOT NULL
);
```

This powers the dashboard notification feed independently of Discord delivery.

### Send Behavior

- **Fire-and-forget** with 5-second timeout on the HTTP POST.
- Failed Discord sends are logged (`[notify] Discord send failed: ...`) but never block the bot loop or raise exceptions.
- No retry on failed Discord sends — the notification is already in the DB; Discord delivery is best-effort.

### Public API

```python
def notify(severity: str, title: str, message: str, fields: dict = None) -> None:
    """Route a notification to the correct Discord channel and persist to DB."""
```

All bot components call this single function. Routing is handled internally based on severity.

---

## 2. Health Monitoring (`health.py`)

### Heartbeat Writer

Every poll cycle, the bot writes `heartbeat.json` to the project root:

```json
{
    "timestamp": "2026-04-12T18:30:00Z",
    "poll_count": 1423,
    "open_positions": 3,
    "balance": 187.50
}
```

This is a simple file overwrite — no append, no rotation, no cleanup needed.

### Crash Detection on Startup

On bot start, `health.py` reads `heartbeat.json`:

- **File missing:** First run. No alert.
- **Last timestamp < 5 minutes ago:** Clean restart (e.g., deploy). No alert.
- **Last timestamp >= 5 minutes ago:** Unclean shutdown. Fire critical Discord alert: *"Bot restarted after unclean shutdown. Last heartbeat: X minutes ago. Poll count at crash: N."*

### Dashboard — Bot-Down Banner

The dashboard reads `heartbeat.json` on each Streamlit page load:

- **Last heartbeat < 3 minutes old:** Bot is running. No banner.
- **Last heartbeat >= 3 minutes old:** Show red banner: *"Bot offline — last seen X minutes ago."*
- **File missing:** Show yellow banner: *"Bot status unknown — no heartbeat file found."*

Uses the same visual pattern as the existing circuit breaker HALTED banner.

### Dashboard — Notification Feed Panel

New panel in the Risk & Controls section of the dashboard:

- Queries `notifications` table for `severity IN ('critical', 'warning')`.
- Displays the 20 most recent, newest first.
- Each row: timestamp, severity icon (red/yellow dot), title, message.
- **"Dismiss All"** button: stores a `dismissed_before` timestamp in Streamlit session state. Notifications older than this timestamp are hidden from the feed. DB rows are not deleted.

---

## 3. API Monitoring (`api_monitor.py`)

### Shared Circuit Breaker

One circuit breaker instance per external API:

| Breaker ID | Protects | Used By |
|---|---|---|
| `clob` | Polymarket CLOB API | `executor/live.py` |
| `open_meteo` | Open-Meteo ensemble/forecast API | `markets/open_meteo.py` |
| `polygon_rpc` | Polygon RPC (claims, allowance) | `chain/claimer.py`, `executor/live.py` |

### State Machine

```
CLOSED ──(3 consecutive retriable failures)──> OPEN
OPEN ──(cooldown expires)──> HALF_OPEN
HALF_OPEN ──(success)──> CLOSED (reset trip count)
HALF_OPEN ──(failure)──> OPEN (escalate cooldown)
```

### Escalating Cooldowns

| Trip # | Cooldown |
|---|---|
| 1st | 2 minutes |
| 2nd | 5 minutes |
| 3rd | 10 minutes |
| 4th | 30 minutes |
| 5th+ | 60 minutes (cap) |

Trip count resets to 0 on first success in HALF_OPEN state.

### Error Classification

**Retriable** (increment failure counter):
- HTTP 5xx (500, 502, 503, 504)
- Connection errors, timeouts
- HTTP 429 (rate limited)

**Non-retriable** (skip retry, fire critical alert immediately):
- HTTP 401, 403 (auth failure)
- Invalid signature / nonce errors
- Malformed request errors

### Integration Pattern

Bot code wraps API calls through the monitor:

```python
result = api_monitor.call("clob", lambda: clob_client.post_order(order))
```

If the breaker is OPEN, the call is skipped and `None` is returned. The caller handles `None` as a skipped operation (already the pattern for most bot passes — they check return values before proceeding).

### Discord Alerts

- **First trip** of any breaker → warning alert to #bot-alerts: *"CLOB API circuit breaker tripped (3 consecutive failures). Cooldown: 2 minutes."*
- **Non-retriable error** → critical alert to #bot-alerts: *"CLOB auth failure — non-retriable. Manual investigation required."*
- **Recovery** (HALF_OPEN → CLOSED) → warning alert to #bot-alerts: *"CLOB API recovered after 10-minute outage."*

### Migration: Open-Meteo Inline Logic

The existing circuit breaker logic in `markets/open_meteo.py` (`_fail_count`, `_last_429_ts`, backoff delays) is migrated to the shared `api_monitor.py` breaker. The `open_meteo` breaker instance replaces the inline state. The 429-specific cooldown (`calibration_temp_pass_cooldown_after_429_minutes`) is preserved as a config value — the breaker's cooldown handles retry timing, but the calibration pass still checks the 429 timestamp to skip unnecessary work.

---

## 4. Daily P&L Digest

### Trigger

Each poll cycle, compare the current UTC date against `_last_digest_date`. If the date has rolled over, compile the previous day's summary and send it. This fires naturally within one poll interval after 00:00 UTC.

### Content

Discord embed to #bot-trades with fields:

- **Account Balance:** current USDC balance
- **Realized P&L:** sum of closed trades for the day
- **Trades:** N opened / N closed / N claimed
- **Open Positions:** count + total unrealized exposure
- **Win/Loss:** W-L record for the day
- **Circuit Breaker Trips:** count (0 if none)
- **API Issues:** count of breaker trips (0 if none)

Also written to `notifications` table as `severity = info`.

### No Separate Thread

Piggybacks on the existing poll loop. One date comparison per cycle — negligible overhead.

---

## 5. CLOB Rate Limiting

### Approach

Simple minimum delay between CLOB API calls, matching the existing pattern used by Open-Meteo (`_INTER_REQUEST_DELAY = 0.25s`) and trader monitor (`API_DELAY = 0.15s`).

### Config

```python
"clob_inter_request_delay": 0.3,  # 300ms minimum between CLOB calls
```

### Implementation

In `executor/live.py`, before each CLOB API call:

```python
elapsed = time.time() - _last_clob_call_ts
if elapsed < CLOB_INTER_REQUEST_DELAY:
    time.sleep(CLOB_INTER_REQUEST_DELAY - elapsed)
_last_clob_call_ts = time.time()
```

If a 429 response is received, the `clob` circuit breaker in `api_monitor.py` handles it (escalating cooldown). The rate limiter prevents 429s from happening in the first place; the breaker is the safety net.

---

## DB Changes

One new table:

```sql
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_severity ON notifications(severity);
CREATE INDEX IF NOT EXISTS idx_notifications_timestamp ON notifications(timestamp DESC);
```

No changes to existing tables.

## Config Additions

| Key | Default | Purpose |
|---|---|---|
| `discord_webhook_alerts` | `""` | Discord webhook URL for #bot-alerts |
| `discord_webhook_trades` | `""` | Discord webhook URL for #bot-trades |
| `clob_inter_request_delay` | `0.3` | Minimum seconds between CLOB API calls |
| `api_breaker_trip_threshold` | `3` | Consecutive failures before breaker trips |
| `api_breaker_cooldowns` | `[120, 300, 600, 1800, 3600]` | Escalating cooldown seconds per trip |
| `heartbeat_stale_threshold` | `300` | Seconds before heartbeat is considered stale (crash detection) |
| `dashboard_bot_down_threshold` | `180` | Seconds before dashboard shows bot-down banner |

## Files Changed

| File | Change |
|---|---|
| `notifications.py` | **New** — Discord client + DB persistence |
| `health.py` | **New** — Heartbeat + crash detection |
| `api_monitor.py` | **New** — Shared circuit breaker |
| `config.py` | Add Discord, rate limit, breaker config keys |
| `db.py` | Add `notifications` table creation + query helpers |
| `weather_bot.py` | Wire heartbeat, digest trigger, notification calls, breaker wrapping |
| `executor/live.py` | Add CLOB rate limiting, wrap API calls with breaker |
| `markets/open_meteo.py` | Migrate inline circuit breaker to shared `api_monitor.py` |
| `weather_risk.py` | Add `notify()` calls alongside existing email alerts |
| `dashboard.py` | Add bot-down banner, notification feed panel, dismiss button |

## Paper Trading Impact

None. All features are additive:
- Discord webhooks silently skip if not configured.
- Heartbeat and dashboard additions work in both modes.
- API breaker for `clob` never triggers in paper mode (no CLOB calls).
- Open-Meteo and Polygon RPC breakers protect both modes.
- CLOB rate limiting only applies in `executor/live.py`.
