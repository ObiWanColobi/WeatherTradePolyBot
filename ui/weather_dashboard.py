"""
Weather Trading Bot — Live Dashboard

Run with:   streamlit run ui/weather_dashboard.py
Bot loop:   python weather_bot.py   (separate terminal / cloud process)

Reads from weather_bot.db — no shared state with the bot beyond the database.
The scanner panel runs on demand (button) since a full scan takes ~30s.
All other panels auto-refresh every 30 seconds from the DB.
"""
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from config import WEATHER, PAPER_STARTING_BALANCE
from weather_risk import RiskManager
from executor.paper import PaperExecutor


def _tr_ens(market_id: str, direction: str) -> str:
    """Return a compact trader ensemble string for display, e.g. 'CONFIRM(3v0)'."""
    if not market_id:
        return "—"
    try:
        c = db.get_trader_consensus(market_id, direction)
        if c["signal"] == "NEUTRAL":
            return "—"
        return f"{c['signal']}({c['same']}v{c['opposite']})"
    except Exception:
        return "—"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Weather Bot",
    page_icon="🌤️",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_INTERVAL = 30   # seconds between auto-refreshes

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hours_left(end_date_str: str) -> float | None:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        h = (end - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(0.0, h)
    except Exception:
        return None


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{h*60:.0f}m"
    return f"{h:.1f}h"


def _edge_tier(edge_score: float | None) -> str:
    if edge_score is None:
        return "—"
    if edge_score >= 30:
        return "STRONG"
    if edge_score >= 15:
        return "EDGE"
    return "WEAK"


def _unreal_pnl(trade: dict) -> float:
    """Unrealized P&L — symmetric for YES and NO now that both hold tokens."""
    current = trade.get("current_price") or trade["fill_price"]
    return (current - trade["fill_price"]) * trade["shares"]


def _parse_threshold(row: dict) -> str:
    """
    Return the threshold string (e.g. '>=19°C' or '<=57°F').
    Uses the stored 'threshold' field if available; falls back to parsing
    the market_name question for trades opened before this field was added.
    """
    stored = row.get("threshold")
    if stored:
        return stored
    q = row.get("market_name", "")
    m = re.search(r"be (\d+(?:\.\d+)?°[CF]) or (higher|lower)", q, re.IGNORECASE)
    if m:
        val, direction = m.groups()
        return f">={val}" if direction.lower() == "higher" else f"<={val}"
    return "—"


_risk_mgr = RiskManager()

# ── Load DB data ──────────────────────────────────────────────────────────────

cash          = db.get_balance() or PAPER_STARTING_BALANCE
all_trades    = db.get_all_trades()
open_trades   = [t for t in all_trades if t["status"] == "open"]
HISTORY_CUTOFF = "2026-04-02T20:52"
closed_trades = [t for t in all_trades if t["status"] == "closed" and (t.get("opened_at") or "") > HISTORY_CUTOFF]
stats         = db.get_stats()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🌤️ Weather Bot")
    st.markdown("**Mode:** 🟡 PAPER TRADING")
    st.divider()

    st.subheader("Config")
    st.caption(f"Max bet:          ${WEATHER.get('kelly_max_bet_usdc', 50):.0f} USDC")
    st.caption(f"Max positions:    {WEATHER.get('decision_max_open_positions', 5)}")
    st.caption(f"Max exposure:     {WEATHER.get('decision_max_exposure_pct', 0.30):.0%}")
    st.caption(f"Min edge (entry): {WEATHER.get('entry_min_edge_pct', 0.10):.0%}")
    st.caption(f"Conviction gate:  {WEATHER.get('entry_min_ensemble_conviction', 0.70):.0%}")
    st.caption(f"Kelly fraction:   {WEATHER.get('kelly_fraction', 0.50):.0%}")

    st.divider()

    st.subheader("Exit Triggers")
    st.caption(f"Ensemble flip:    >{WEATHER.get('exit_ensemble_flip_threshold', 0.25):.0%} shift")
    st.caption(f"Adverse move:     >{WEATHER.get('exit_adverse_price_move_pct', 0.30):.0%} of fill")
    st.caption(f"Max spread:       ${WEATHER.get('exit_max_spread_cents', 0.08):.2f}")
    st.caption(f"Hold window:      <{WEATHER.get('exit_no_exit_hours_to_close', 2.0):.0f}h to close")

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

    st.divider()

    if st.button("🔄 Refresh Now"):
        st.rerun()

    st.divider()
    st.subheader("⚠️ Reset Paper Trading")
    summary = db.get_session_summary()
    st.caption(
        f"{summary['open_count']} open · {summary['closed_count']} closed · "
        f"${summary['balance']:.2f} balance"
    )
    if "confirm_reset" not in st.session_state:
        st.session_state.confirm_reset = False

    if not st.session_state.confirm_reset:
        if st.button("🗑️ Reset", type="secondary"):
            st.session_state.confirm_reset = True
            st.rerun()
    else:
        st.warning("This will wipe all trades and reset the balance.")
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

unrealized    = sum(_unreal_pnl(t) for t in open_trades)
position_val  = sum((t.get("current_price") or t["fill_price"]) * t["shares"] for t in open_trades)
account_value = cash + position_val
acct_pnl      = account_value - PAPER_STARTING_BALANCE
acct_pnl_pct  = (acct_pnl / PAPER_STARTING_BALANCE * 100) if PAPER_STARTING_BALANCE else 0
realized_pnl  = stats["total_pnl"]
realized_pct  = (realized_pnl / PAPER_STARTING_BALANCE * 100) if PAPER_STARTING_BALANCE else 0
today_str     = datetime.now(timezone.utc).date().isoformat()
trades_today  = sum(1 for t in all_trades if (t.get("opened_at") or "").startswith(today_str))

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🌤️ Weather Trading Bot — Paper Mode")
st.caption(f"Auto-refreshes every {REFRESH_INTERVAL}s · last loaded {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

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

# ── Top metrics ───────────────────────────────────────────────────────────────

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    st.metric("Account Value", f"${account_value:,.2f}", delta=f"{acct_pnl_pct:+.1f}%")
with c2:
    st.metric("Realized P&L", f"${realized_pnl:+.2f}", delta=f"{realized_pct:+.1f}%")
with c3:
    st.metric("Unrealized P&L", f"${unrealized:+.2f}")
with c4:
    st.metric("Win Rate", f"{stats['win_rate']:.1f}%",
              delta=f"{stats['wins']}W / {stats['losses']}L", delta_color="off")
with c5:
    st.metric("Open Positions", len(open_trades))
with c6:
    st.metric("Trades Today", trades_today)

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
        y=PAPER_STARTING_BALANCE, line_dash="dash", line_color="gray",
        annotation_text=f"Start ${PAPER_STARTING_BALANCE:,.0f}",
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

# ── Open positions ────────────────────────────────────────────────────────────

st.subheader(f"Open Positions ({len(open_trades)})")

if open_trades:
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
            ens_str = f"{ens_pct:.0%}"   # fallback for old trades without raw counts
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

    # Per-trade close buttons — horizontal row of buttons, confirmation below
    st.caption("Manual close:")
    _btn_cols = st.columns(min(len(open_trades), 4))
    _pending_close = None  # track which trade (if any) is awaiting confirmation
    for i, t in enumerate(open_trades):
        trade_id = t["id"]
        city = (t.get("city") or "?").title()
        direction = t.get("direction", "?").upper()
        threshold = _parse_threshold(t)
        state_key = f"confirm_close_{trade_id}"
        time_key = f"close_time_{trade_id}"

        if state_key not in st.session_state:
            st.session_state[state_key] = False
        if time_key not in st.session_state:
            st.session_state[time_key] = None

        # Expire stale confirmations
        if st.session_state[state_key]:
            elapsed = time.time() - (st.session_state[time_key] or 0)
            if elapsed > 10:
                st.session_state[state_key] = False

        with _btn_cols[i % 4]:
            label = f"Close: {city} {direction} {threshold}"
            btn_type = "primary" if st.session_state[state_key] else "secondary"
            if st.button(label, key=f"btn_close_{trade_id}", type=btn_type):
                # Toggle: click again to cancel pending confirmation
                st.session_state[state_key] = not st.session_state[state_key]
                st.session_state[time_key] = time.time() if st.session_state[state_key] else None
                st.rerun()

        if st.session_state[state_key]:
            _pending_close = (t, trade_id, city, direction, threshold, state_key)

    # Confirmation UI — full width, below all buttons
    if _pending_close:
        t, trade_id, city, direction, threshold, state_key = _pending_close
        st.warning(f"Confirm close: **{city} {direction} {threshold}**? (auto-cancels in 10s)")
        c1, c2, _ = st.columns([1, 1, 6])
        with c1:
            if st.button("✅ Confirm", key=f"btn_confirm_{trade_id}", type="primary"):
                executor = PaperExecutor()
                executor.close_full(t, reason="manual_close")
                _risk_mgr.add_manual_close(t.get("market_id", ""))
                print(f"[risk] manual close: {t.get('market_name', '')[:50]}")
                st.session_state[state_key] = False
                st.success(f"Closed {city} {direction} {threshold}.")
                time.sleep(1)
                st.rerun()
        with c2:
            if st.button("❌ Cancel", key=f"btn_cancel_{trade_id}"):
                st.session_state[state_key] = False
                st.rerun()
else:
    st.info("No open positions.")

st.divider()

# ── Live scanner ──────────────────────────────────────────────────────────────

st.subheader("Live Scanner")
st.caption("Results are written by the bot after each poll — no separate API calls needed.")

scan_results, scan_cached_at = db.get_scan_cache()

if scan_cached_at:
    try:
        cached_dt = datetime.fromisoformat(scan_cached_at)
        age_s = (datetime.now(timezone.utc) - cached_dt).total_seconds()
        age_str = f"{int(age_s // 60)}m {int(age_s % 60)}s ago"
    except Exception:
        age_str = "unknown"
    st.caption(f"Last bot scan: {scan_cached_at[:19]} UTC · {age_str}")

if scan_results:
    rows = []
    for r in scan_results:
        ens_yes = r.get("ens_yes")
        ens_n   = r.get("ens_n") or 0
        ens_str = f"{int(ens_yes)}/{ens_n}" if ens_yes is not None and ens_n else "—"
        url = r.get("market_url") or ""
        direction = "YES" if r["model_prob"] > r["market_price"] else "NO"
        rows.append({
            "City":       r["city_display"],
            "Resolves":   r["resolves_str"],
            "Market":     r["threshold_str"],
            "Vol 24h":    round(r["volume"]),
            "Mkt %":      round(r["market_price"] * 100, 1),
            "Model %":    round(r["model_prob"] * 100, 1),
            "Edge %":     round(r["edge_pct"] * 100, 1),
            "Tier":       "STRONG" if r["edge_pct"] >= 0.30 else ("EDGE" if r["edge_pct"] >= 0.15 else "WEAK"),
            "Ensemble":   ens_str,
            "Tr Ens":     _tr_ens(r.get("market_id", ""), direction),
            "Conviction": "✅" if r["conviction_ok"] else "⚠️",
            "Closes In":  f"{r['hours_to_close']:.1f}h" if r.get("hours_to_close") is not None else "—",
            "Signal":     f"BUY {direction}",
            "Link":       url,
        })

    df_scan    = pd.DataFrame(rows)
    tradeable  = df_scan[df_scan["Conviction"] == "✅"]
    uncertain  = df_scan[df_scan["Conviction"] == "⚠️"]

    st.markdown(
        f"**{len(tradeable)} tradeable** · "
        f"{sum(1 for r in scan_results if r['conviction_ok'] and r['edge_pct'] >= 0.30)} strong · "
        f"{sum(1 for r in scan_results if r['conviction_ok'] and 0.15 <= r['edge_pct'] < 0.30)} edge · "
        f"{len(uncertain)} skipped (uncertain ensemble)"
    )
    scan_col_cfg = {
        "Vol 24h": st.column_config.NumberColumn(format="$%d"),
        "Mkt %":   st.column_config.NumberColumn(format="%.1f%%"),
        "Model %": st.column_config.NumberColumn(format="%.1f%%"),
        "Edge %":  st.column_config.NumberColumn(format="%.1f%%"),
    }
    if any(r.get("market_url", "").startswith("http") for r in scan_results):
        scan_col_cfg["Link"] = st.column_config.LinkColumn(
            "Link", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_scan, column_config=scan_col_cfg, width='stretch', hide_index=True)
elif scan_cached_at:
    st.info("No opportunities found in last scan.")
else:
    st.info("Waiting for first bot poll — scanner results will appear here automatically.")

st.divider()

# ── Trade history ─────────────────────────────────────────────────────────────

st.subheader(f"Trade History ({len(closed_trades)} closed)")

if closed_trades:
    rows = []
    for t in closed_trades:
        url = t.get("market_url") or ""
        ens_yes = t.get("entry_ensemble_yes")
        ens_n   = t.get("entry_ensemble_n")
        ens_pct = t.get("entry_ensemble_pct")
        if ens_yes is not None and ens_n:
            ens_str = f"{int(ens_yes)}/{int(ens_n)}"
        elif ens_pct is not None:
            ens_str = f"{ens_pct:.0%}"
        else:
            ens_str = "—"
        rows.append({
            "City":         (t.get("city") or "—").title(),
            "Threshold":    _parse_threshold(t),
            "Bet":          t["direction"].title(),
            "Tier":         _edge_tier(t.get("edge_score")),
            "Edge %":       round(t.get("edge_score") or 0, 1),
            "Model %":      round((t.get("estimated_prob") or 0) * 100, 1),
            "Mkt %":        round((t.get("entry_price") or 0) * 100, 1),
            "Ens. Entry":   ens_str,
            "Tr Ens":       _tr_ens(t.get("market_id", ""), t.get("direction", "")),
            "Fill":         round(t["fill_price"], 3),
            "Exit":         round(t.get("exit_price") or 0, 3),
            "P&L $":        round(t.get("pnl") or 0, 2),
            "P&L %":        round(t.get("pnl_pct") or 0, 1),
            "Size $":       round(t["size_usdc"], 2),
            "Hrs @ Entry":  _fmt_hours(t.get("hours_to_close_at_entry")),
            "Hrs @ Exit":   _fmt_hours(t.get("hours_to_close_at_exit")),
            "Vol 24h":      round(t["volume_24h"]) if t.get("volume_24h") else None,
            "Exit Reason":  (t.get("exit_reason") or "resolved")[:40],
            "Market":       url or t.get("market_name", ""),
            "Opened":       (t.get("opened_at") or "")[:16],
            "Closed":       (t.get("closed_at") or "")[:16],
        })

    df_closed = pd.DataFrame(rows)
    closed_col_cfg = {
        "Edge %":  st.column_config.NumberColumn(format="%.1f%%"),
        "Model %": st.column_config.NumberColumn(format="%.1f%%"),
        "Mkt %":   st.column_config.NumberColumn(format="%.1f%%"),
        "P&L $":   st.column_config.NumberColumn(format="$%.2f"),
        "P&L %":   st.column_config.NumberColumn(format="%.1f%%"),
        "Size $":  st.column_config.NumberColumn(format="$%.2f"),
        "Vol 24h": st.column_config.NumberColumn(format="$%d"),
    }
    if any(r["Market"].startswith("http") for r in rows):
        closed_col_cfg["Market"] = st.column_config.LinkColumn(
            "Market", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_closed, column_config=closed_col_cfg, width='stretch', hide_index=True)

    st.divider()

    # ── Performance breakdown ─────────────────────────────────────────────────

    st.subheader("Performance Breakdown")
    col_a, col_b, col_c = st.columns(3)

    def _win_rate_table(groups: dict) -> pd.DataFrame:
        rows = []
        for label, trades in groups.items():
            wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
            n    = len(trades)
            pnl  = sum(t.get("pnl") or 0 for t in trades)
            rows.append({
                "Group":    label,
                "Trades":   n,
                "Wins":     wins,
                "Win Rate": f"{wins/n*100:.0f}%" if n else "—",
                "Net P&L":  round(pnl, 2),
            })
        return pd.DataFrame(rows)

    with col_a:
        st.markdown("**By Direction**")
        groups = {
            "YES": [t for t in closed_trades if t["direction"] == "YES"],
            "NO":  [t for t in closed_trades if t["direction"] == "NO"],
        }
        st.dataframe(_win_rate_table(groups), width='stretch', hide_index=True)

    with col_b:
        st.markdown("**By Edge Tier**")
        groups = {
            "STRONG (≥30%)": [t for t in closed_trades if (t.get("edge_score") or 0) >= 30],
            "EDGE (15-30%)": [t for t in closed_trades if 15 <= (t.get("edge_score") or 0) < 30],
            "WEAK (<15%)":   [t for t in closed_trades if (t.get("edge_score") or 0) < 15],
        }
        st.dataframe(_win_rate_table(groups), width='stretch', hide_index=True)

    with col_c:
        st.markdown("**By Exit Reason**")
        reason_groups: dict = {}
        for t in closed_trades:
            r = (t.get("exit_reason") or "resolved").split("→")[0].strip()
            # Normalise to short labels
            if "resolved" in r:
                r = "resolved"
            elif "ensemble" in r:
                r = "ensemble flip"
            elif "adverse" in r:
                r = "adverse move"
            elif "spread" in r:
                r = "spread"
            reason_groups.setdefault(r, []).append(t)
        st.dataframe(_win_rate_table(reason_groups), width='stretch', hide_index=True)

    # P&L chart
    if len(closed_trades) > 1:
        st.markdown("**P&L per Trade**")
        pnl_vals  = [t.get("pnl") or 0 for t in closed_trades]
        trade_labels = [
            f"{(t.get('city') or '?').title()} {_parse_threshold(t)} {t['direction']}"
            for t in closed_trades
        ]
        colors = ["#00cc88" if v >= 0 else "#ff4444" for v in pnl_vals]
        fig_pnl = go.Figure(go.Bar(
            x=trade_labels, y=pnl_vals,
            marker_color=colors,
            hovertemplate="%{x}<br>P&L: $%{y:.2f}<extra></extra>",
        ))
        fig_pnl.update_layout(
            height=240, margin=dict(l=0, r=0, t=10, b=0),
            yaxis_tickprefix="$",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False), yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
            showlegend=False,
        )
        st.plotly_chart(fig_pnl, width='stretch')

else:
    st.info("No closed trades yet — P&L breakdown will appear here after first resolution.")

# ── Trader Accuracy (Stage 2) ────────────────────────────────────────────────
st.subheader("Trader Accuracy")

ta_col1, ta_col2 = st.columns([1, 1])
with ta_col1:
    min_resolved_filter = st.slider(
        "Min resolved forecasts", min_value=1, max_value=50, value=10, key="ta_min_resolved"
    )
with ta_col2:
    exclude_coinflip = st.checkbox(
        "Exclude coin-flip zone (|delta| < 2 deg C)",
        value=False,
        key="ta_exclude_coinflip",
    )

delta_threshold = 2.0 if exclude_coinflip else None

ta_rows = db.get_trader_accuracy_summary(
    min_resolved=min_resolved_filter,
    require_temp_delta_ge=delta_threshold,
)

if not ta_rows:
    st.info("No resolved trader forecasts yet. Table will populate as markets resolve.")
else:
    import pandas as _pd
    df = _pd.DataFrame(ta_rows)
    df["accuracy_%"] = (df["accuracy"] * 100).round(1)
    df["brier"]      = df["brier"].round(4) if "brier" in df else None
    df["wallet_short"] = df["wallet"].str[:6] + "..." + df["wallet"].str[-4:]
    display = df[[
        "wallet_short", "pseudonym", "n_forecasts", "n_resolved", "accuracy_%",
        "brier", "best_city", "worst_city", "best_season",
        "edge_tier_breakdown", "last_forecast",
    ]].rename(columns={
        "wallet_short":        "Wallet",
        "pseudonym":           "Pseudonym",
        "n_forecasts":         "N",
        "n_resolved":          "Resolved",
        "accuracy_%":          "Accuracy %",
        "brier":               "Brier",
        "best_city":           "Best City",
        "worst_city":          "Worst City",
        "best_season":         "Best Season",
        "edge_tier_breakdown": "Edge Tier (W/E/S)",
        "last_forecast":       "Last Forecast",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)

# ── Auto-refresh ──────────────────────────────────────────────────────────────

time.sleep(REFRESH_INTERVAL)
st.rerun()
