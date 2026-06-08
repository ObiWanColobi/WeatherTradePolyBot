"""
Weather Trading Bot — Live Dashboard (shotgun v1)

Run with:   streamlit run ui/weather_dashboard.py
Bot loop:   python weather_bot.py   (separate terminal / cloud process)

Reads from weather_bot.db — no shared state with the bot beyond the database.

v1 shows the SHOTGUN data model: one "fire" per (city, resolution-date) with a
rolled-up P&L, expandable to the individual per-bucket leg bets. The retired
threshold-era views (per-market scanner, single-trade positions, trader
accuracy, edge-tier breakdowns) were dropped — the shotgun bot fires a spread
of legs internally and holds them to resolution, so there is no per-market
scanner or per-position close in v1.
"""
import os as _os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import health as _health
from config import SHOTGUN, PAPER_STARTING_BALANCE, TRADING_MODE

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Weather Bot",
    page_icon="🌤️",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_INTERVAL = 30   # seconds between auto-refreshes

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h*60:.0f}m"
    return f"{h:.1f}h"


# ── Load DB data ──────────────────────────────────────────────────────────────

cash          = db.get_balance() or PAPER_STARTING_BALANCE
fires         = db.get_fires_with_rollup()
active_fires  = [f for f in fires if f.get("status") == "open" or (f.get("open_legs") or 0) > 0]
closed_fires  = [f for f in fires if f not in active_fires]

open_exposure = db.get_open_exposure()
# Realized P&L = sum of P&L on all resolved (closed) legs across every fire.
realized_pnl  = sum((f.get("pnl") or 0.0) for f in fires)

# Account value: cash on hand + the staked-but-not-yet-resolved capital sitting
# in open legs. We do NOT mark open legs to a live mid in v1 (legs aren't price-
# refreshed once fired), so open exposure is carried at cost — no fake unrealized.
account_value = cash + open_exposure
_start_balance = PAPER_STARTING_BALANCE
acct_pnl      = account_value - _start_balance
acct_pnl_pct  = (acct_pnl / _start_balance * 100) if _start_balance else 0.0
realized_pct  = (realized_pnl / _start_balance * 100) if _start_balance else 0.0

total_wins    = sum((f.get("wins") or 0) for f in fires)
total_resolved_legs = sum(((f.get("legs") or 0) - (f.get("open_legs") or 0)) for f in fires)
win_rate      = (total_wins / total_resolved_legs * 100) if total_resolved_legs else 0.0

today_str     = datetime.now(timezone.utc).date().isoformat()
fires_today   = sum(1 for f in fires if (f.get("fired_at_utc") or "").startswith(today_str))

_exposure_cap_pct = SHOTGUN.get("portfolio_exposure_cap_pct", 0.80)
_exposure_cap     = account_value * _exposure_cap_pct

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🌤️ Weather Bot")
    if TRADING_MODE == "live":
        st.markdown("**Mode:** 🔴 LIVE TRADING")
    else:
        st.markdown("**Mode:** 🟡 PAPER TRADING")
    st.divider()

    st.subheader("Shotgun Config")
    st.caption(f"Mode:             {SHOTGUN.get('mode', '—')}")
    st.caption(f"Fire window:      {SHOTGUN.get('fire_window_hours', 0):.0f}h to close")
    st.caption(f"Budget/city-day:  ${SHOTGUN.get('budget_per_city_day', 0):.2f}")
    st.caption(f"Edge threshold:   {SHOTGUN.get('edge_threshold', 0):.2f}")
    st.caption(f"Mass-core frac:   {SHOTGUN.get('mass_core_frac', 0):.0%}")
    st.caption(f"Exposure cap:     {SHOTGUN.get('portfolio_exposure_cap_pct', 0):.0%} of account")
    st.caption(f"Price band:       {SHOTGUN.get('price_min', 0):.2f}–{SHOTGUN.get('price_max', 0):.2f}")
    st.caption(f"Vol floor:        ${SHOTGUN.get('vol_min', 0):.0f}")
    st.caption(f"Cities:           {len(SHOTGUN.get('cities', []))}")

    st.divider()

    st.subheader("Exposure")
    st.caption(f"Open exposure: ${open_exposure:,.2f}")
    st.caption(f"Cap ({_exposure_cap_pct:.0%}): ${_exposure_cap:,.2f}")

    st.divider()

    if st.button("🔄 Refresh Now"):
        st.rerun()

    st.divider()
    st.subheader("⚠️ Reset Paper Trading")
    st.caption(
        f"{len(active_fires)} active · {len(closed_fires)} closed fires · "
        f"${cash:,.2f} cash"
    )
    st.caption("Resets balance + balance history. (Fires/bets are managed by the bot.)")
    if "confirm_reset" not in st.session_state:
        st.session_state.confirm_reset = False

    if not st.session_state.confirm_reset:
        if st.button("🗑️ Reset", type="secondary"):
            st.session_state.confirm_reset = True
            st.rerun()
    else:
        st.warning("This will reset the balance to its starting value.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Confirm", type="primary"):
                db.reset_paper_trading()
                st.session_state.confirm_reset = False
                st.success("Reset complete.")
                time.sleep(1)
                st.rerun()
        with c2:
            if st.button("❌ Cancel"):
                st.session_state.confirm_reset = False
                st.rerun()

# ── Header ────────────────────────────────────────────────────────────────────

_mode_title = "Live Mode" if TRADING_MODE == "live" else "Paper Mode"
st.title(f"🌤️ Weather Trading Bot — {_mode_title}")
st.caption(
    f"Auto-refreshes every {REFRESH_INTERVAL}s · "
    f"last loaded {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
)

# ── Bot status banner ─────────────────────────────────────────────────────────

_heartbeat_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "heartbeat.json")
_bot_status = _health.get_bot_status(_heartbeat_path, down_threshold=180)
if not _bot_status["running"]:
    if _bot_status.get("no_file"):
        st.warning("**Bot status unknown** — no heartbeat file found.")
    else:
        st.error(f"**Bot offline** — last seen {_bot_status['minutes_ago']} minutes ago.")

# ── Notification Feed ─────────────────────────────────────────────────────────

_DISMISS_FILE = Path(__file__).parent.parent / "data" / "dismissed_alerts.txt"


def _load_dismissed_before() -> str | None:
    try:
        return _DISMISS_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _save_dismissed_before(ts: str):
    _DISMISS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DISMISS_FILE.write_text(ts)


if "dismissed_before" not in st.session_state:
    st.session_state.dismissed_before = _load_dismissed_before()

_MAX_ALERTS = 5
try:
    _notif_rows = db.get_notifications(
        severity_in=["critical", "warning"],
        limit=_MAX_ALERTS,
        since=st.session_state.dismissed_before,
    )
except Exception:
    _notif_rows = []
if _notif_rows:
    with st.expander(f"⚠️ Alerts ({len(_notif_rows)})", expanded=True):
        if st.button("Dismiss All"):
            ts = datetime.now(timezone.utc).isoformat()
            st.session_state.dismissed_before = ts
            _save_dismissed_before(ts)
            st.rerun()
        for row in _notif_rows:
            icon = "🔴" if row["severity"] == "critical" else "🟡"
            ts_short = row["timestamp"][:19].replace("T", " ")
            st.markdown(f"{icon} **{ts_short}** — {row['title']}: {row['message']}")

# ── Top metrics ───────────────────────────────────────────────────────────────

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    st.metric("Account Value", f"${account_value:,.2f}", delta=f"{acct_pnl_pct:+.1f}%")
with c2:
    st.metric("Cash", f"${cash:,.2f}")
with c3:
    st.metric("Open Exposure", f"${open_exposure:,.2f}",
              delta=f"cap {_exposure_cap_pct:.0%}", delta_color="off")
with c4:
    st.metric("Realized P&L", f"${realized_pnl:+.2f}", delta=f"{realized_pct:+.1f}%")
with c5:
    st.metric("Leg Win Rate", f"{win_rate:.1f}%",
              delta=f"{total_wins}W / {total_resolved_legs} resolved", delta_color="off")
with c6:
    st.metric("Active Fires", len(active_fires), delta=f"{fires_today} today", delta_color="off")

st.caption(
    "Account Value = cash + open exposure carried **at cost** (open legs are not "
    "marked to a live price in v1). Realized P&L is the sum of resolved-leg P&L. "
    "No unrealized P&L is shown — it isn't tracked for held legs."
)

st.divider()

# ── Balance chart ─────────────────────────────────────────────────────────────

st.subheader("Account Value Over Time")
balance_hist = db.get_balance_history()

if balance_hist:
    df_bal = pd.DataFrame(balance_hist)
    df_bal["recorded_at"] = pd.to_datetime(df_bal["recorded_at"], format="mixed", utc=True)
    df_bal = df_bal.sort_values("recorded_at")
    df_bal = df_bal.set_index("recorded_at").resample("5min").last().dropna().reset_index()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_bal["recorded_at"], y=df_bal["amount"],
        mode="lines+markers",
        line=dict(color="#00cc88", width=2),
        marker=dict(size=4),
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>$%{y:,.2f}<extra></extra>",
    ))
    fig.add_hline(
        y=_start_balance, line_dash="dash", line_color="gray",
        annotation_text=f"Start ${_start_balance:,.0f}",
    )
    fig.update_layout(
        height=260, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_tickprefix="$",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=False),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
    )
    st.plotly_chart(fig, width='stretch')
else:
    st.info("No account value history yet — start the bot with `python weather_bot.py`")

st.divider()

# ── Fire rendering helpers ─────────────────────────────────────────────────────


def _fire_summary_row(f: dict) -> dict:
    return {
        "City":       (f.get("city") or "—").title(),
        "Resolves":   f.get("resolution_date") or "—",
        "Fired":      (f.get("fired_at_utc") or "")[:16].replace("T", " "),
        "Lead":       _fmt_hours(f.get("lead_hours")),
        "Center °F":  round(f["center_f"], 1) if f.get("center_f") is not None else None,
        "Legs":       f.get("legs") or 0,
        "Open Legs":  f.get("open_legs") or 0,
        "Wins":       f.get("wins") or 0,
        "Staked $":   round(f.get("staked") or 0.0, 2),
        "P&L $":      round(f.get("pnl") or 0.0, 2),
    }


_FIRE_COL_CFG = {
    "Center °F": st.column_config.NumberColumn(format="%.1f"),
    "Staked $":  st.column_config.NumberColumn(format="$%.2f"),
    "P&L $":     st.column_config.NumberColumn(format="$%.2f"),
}


def _render_legs(fire_id: int):
    legs = db.get_all_bets_for_fire(fire_id)
    if not legs:
        st.caption("No legs recorded for this fire.")
        return
    rows = []
    for b in sorted(legs, key=lambda x: (x.get("ladder_idx") if x.get("ladder_idx") is not None else 0)):
        rows.append({
            "Bucket":   b.get("group_item_title") or "—",
            "Side":     (b.get("side") or "—").upper(),
            "Ladder":   b.get("ladder_idx"),
            "Density":  round(b["density"], 3) if b.get("density") is not None else None,
            "Edge":     round(b["edge"], 3) if b.get("edge") is not None else None,
            "Mid":      round(b["mid_price"], 3) if b.get("mid_price") is not None else None,
            "Fill":     round(b["fill_price"], 3) if b.get("fill_price") is not None else None,
            "Shares":   round(b["shares"], 2) if b.get("shares") is not None else None,
            "Stake $":  round(b["stake_usd"], 2) if b.get("stake_usd") is not None else None,
            "Status":   b.get("status") or "—",
            "Outcome":  b.get("resolved_outcome") or "—",
            "P&L $":    round(b["pnl"], 2) if b.get("pnl") is not None else None,
        })
    leg_cfg = {
        "Density":  st.column_config.NumberColumn(format="%.3f"),
        "Edge":     st.column_config.NumberColumn(format="%.3f"),
        "Mid":      st.column_config.NumberColumn(format="%.3f"),
        "Fill":     st.column_config.NumberColumn(format="%.3f"),
        "Stake $":  st.column_config.NumberColumn(format="$%.2f"),
        "P&L $":    st.column_config.NumberColumn(format="$%.2f"),
    }
    st.dataframe(pd.DataFrame(rows), column_config=leg_cfg,
                 width='stretch', hide_index=True)


# ── Active fires ───────────────────────────────────────────────────────────────

st.subheader(f"Active Fires ({len(active_fires)})")

if active_fires:
    df_active = pd.DataFrame([_fire_summary_row(f) for f in active_fires])
    st.dataframe(df_active, column_config=_FIRE_COL_CFG, width='stretch', hide_index=True)

    st.caption("Expand a fire to see its individual leg bets:")
    for f in active_fires:
        title = (
            f"🔥 {(f.get('city') or '—').title()} · {f.get('resolution_date') or '—'} · "
            f"{f.get('open_legs') or 0}/{f.get('legs') or 0} open · "
            f"staked ${f.get('staked') or 0:.2f} · P&L ${f.get('pnl') or 0:+.2f}"
        )
        with st.expander(title):
            _render_legs(f["id"])
else:
    st.info("No active fires.")

st.divider()

# ── Closed fires ───────────────────────────────────────────────────────────────

st.subheader(f"Closed Fires ({len(closed_fires)})")

if closed_fires:
    total_closed_pnl = sum((f.get("pnl") or 0.0) for f in closed_fires)
    st.markdown(f"**Total closed P&L: ${total_closed_pnl:+,.2f}**")

    df_closed = pd.DataFrame([_fire_summary_row(f) for f in closed_fires])
    st.dataframe(df_closed, column_config=_FIRE_COL_CFG, width='stretch', hide_index=True)

    st.caption("Expand a closed fire to see its resolved legs:")
    for f in closed_fires:
        title = (
            f"✅ {(f.get('city') or '—').title()} · {f.get('resolution_date') or '—'} · "
            f"{f.get('wins') or 0}/{f.get('legs') or 0} wins · "
            f"staked ${f.get('staked') or 0:.2f} · P&L ${f.get('pnl') or 0:+.2f}"
        )
        with st.expander(title):
            _render_legs(f["id"])
else:
    st.info("No closed fires yet — fires move here once all legs resolve.")

# ── Auto-refresh ──────────────────────────────────────────────────────────────

time.sleep(REFRESH_INTERVAL)
st.rerun()
