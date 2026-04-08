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
