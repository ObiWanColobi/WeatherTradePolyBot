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

# ── Load DB data ──────────────────────────────────────────────────────────────

cash          = db.get_balance() or PAPER_STARTING_BALANCE
all_trades    = db.get_all_trades()
open_trades   = [t for t in all_trades if t["status"] == "open"]
HISTORY_CUTOFF = "2026-04-02T20:52"
closed_trades = [t for t in all_trades if t["status"] == "closed" and (t.get("opened_at") or "") > HISTORY_CUTOFF]
stats         = db.get_stats()

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
            "Unreal. P&L":  round(unreal, 2),
            "Size $":       round(t["size_usdc"], 2),
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
        "Unreal. P&L": st.column_config.NumberColumn(format="$%.2f"),
        "Vol 24h":     st.column_config.NumberColumn(format="$%d"),
    }
    if any(r["Market"].startswith("http") for r in rows):
        col_cfg["Market"] = st.column_config.LinkColumn(
            "Market", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_open, column_config=col_cfg, width='stretch', hide_index=True)
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
        rows.append({
            "City":         (t.get("city") or "—").title(),
            "Threshold":    _parse_threshold(t),
            "Bet":          t["direction"].title(),
            "Tier":         _edge_tier(t.get("edge_score")),
            "Edge %":       round(t.get("edge_score") or 0, 1),
            "Model %":      round((t.get("estimated_prob") or 0) * 100, 1),
            "Mkt %":        round((t.get("entry_price") or 0) * 100, 1),
            "Tr Ens":       _tr_ens(t.get("market_id", ""), t.get("direction", "")),
            "Fill":         round(t["fill_price"], 3),
            "Exit":         round(t.get("exit_price") or 0, 3),
            "P&L $":        round(t.get("pnl") or 0, 2),
            "P&L %":        round(t.get("pnl_pct") or 0, 1),
            "Size $":       round(t["size_usdc"], 2),
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

# ── Auto-refresh ──────────────────────────────────────────────────────────────

time.sleep(REFRESH_INTERVAL)
st.rerun()
