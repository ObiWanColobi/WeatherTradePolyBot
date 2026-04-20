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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from config import WEATHER, PAPER_STARTING_BALANCE, LIVE_STARTING_BALANCE, TRADING_MODE, apply_live_overrides

apply_live_overrides()
from weather_risk import RiskManager
from executor import create_executor
from notifications import notify


@st.cache_resource
def _get_executor():
    """Cached executor — initializes once per Streamlit session."""
    try:
        return create_executor()
    except Exception as e:
        print(f"[dashboard] Executor init failed: {e}")
        return None


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
# Include claim_pending rows (resolved, awaiting on-chain redemption) in the
# history view so they're visible while the claim completes. Once the claim
# confirms they transition to status='closed' and continue to render here.
closed_trades = [
    t for t in all_trades
    if t["status"] in ("closed", "claim_pending")
    and (t.get("opened_at") or "") > HISTORY_CUTOFF
]
stats         = db.get_stats()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🌤️ Weather Bot")
    if TRADING_MODE == "live":
        st.markdown("**Mode:** 🔴 LIVE TRADING")
    else:
        st.markdown("**Mode:** 🟡 PAPER TRADING")
    st.divider()

    st.subheader("Config")
    st.caption(f"Max bet:          ${WEATHER['kelly_max_bet_usdc']:.0f} USDC")
    st.caption(f"Max positions:    {WEATHER['decision_max_open_positions']}")
    st.caption(f"Max exposure:     {WEATHER['decision_max_exposure_pct']:.0%}")
    st.caption(f"Min edge (entry): {WEATHER['entry_min_edge_pct']:.0%}")
    st.caption(f"Conviction gate:  {WEATHER['entry_min_ensemble_conviction']:.0%}")
    st.caption(f"Kelly fraction:   {WEATHER['kelly_fraction']:.0%}")
    st.caption(f"Entry cutoff:     >{WEATHER['entry_min_hours_to_close']:.0f}h to close")

    st.divider()

    st.subheader("Exit Triggers")
    st.caption(f"Ensemble flip:    >{WEATHER['exit_ensemble_flip_threshold']:.0%} shift")
    st.caption(f"Adverse move:     >{WEATHER['exit_adverse_price_move_pct']:.0%} of fill")
    st.caption(
        f"Late-game:        <{WEATHER['exit_late_game_hours']:.0f}h left, "
        f"mkt<{WEATHER['exit_late_game_market_floor']:.0%}, "
        f"ens≥{WEATHER['exit_late_game_ensemble_threshold']:.0%}"
    )

    st.divider()

    # ── Risk Status ──────────────────────────────────────────────────────────
    st.subheader("Risk Status")
    loss_count, loss_total = db.get_today_realized_losses()
    st.caption(f"Losses today: {loss_count} trades, ${loss_total:.2f}")
    st.caption(f"Loss limit: {WEATHER['risk_daily_loss_limit_pct']:.0%} of account")
    if TRADING_MODE == "live":
        st.warning("🔴 LIVE mode — closes execute real CLOB orders")
    else:
        st.info("📄 Paper mode")

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
                        _risk_mgr.send_alert(
                            "manual_close_all",
                            f"All positions closed manually.\n\n"
                            f"Closed {len(closed_ids)} positions:\n" +
                            "\n".join(f"  - {name}" for name in closed_ids),
                        )
                        notify("critical", "Manual Close All",
                               "All positions closed manually.",
                               fields={"Positions Closed": str(len(closed_ids))})
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

pending_trades = [t for t in all_trades if t["status"] == "claim_pending"]
unrealized    = sum(_unreal_pnl(t) for t in open_trades)
position_val  = sum((t.get("current_price") or t["fill_price"]) * t["shares"] for t in open_trades)
pending_val   = sum(t["shares"] for t in pending_trades)
account_value = cash + position_val + pending_val
if TRADING_MODE == "live":
    _start_balance = LIVE_STARTING_BALANCE
else:
    _start_balance = PAPER_STARTING_BALANCE

acct_pnl      = account_value - _start_balance
acct_pnl_pct  = (acct_pnl / _start_balance * 100) if _start_balance else 0
realized_pnl  = stats["total_pnl"]
realized_pct  = (realized_pnl / _start_balance * 100) if _start_balance else 0
today_str     = datetime.now(timezone.utc).date().isoformat()
trades_today  = sum(1 for t in all_trades if (t.get("opened_at") or "").startswith(today_str))

# ── Header ────────────────────────────────────────────────────────────────────

_mode_title = "Live Mode" if TRADING_MODE == "live" else "Paper Mode"
st.title(f"🌤️ Weather Trading Bot — {_mode_title}")
st.caption(f"Auto-refreshes every {REFRESH_INTERVAL}s · last loaded {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

# ── Bot status banner ─────────────────────────────────────────────────────────

import os as _os
import health as _health

_heartbeat_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "heartbeat.json")
_bot_status = _health.get_bot_status(
    _heartbeat_path,
    down_threshold=WEATHER.get("dashboard_bot_down_threshold", 180),
)
if not _bot_status["running"]:
    if _bot_status.get("no_file"):
        st.warning("**Bot status unknown** — no heartbeat file found.")
    else:
        st.error(f"**Bot offline** — last seen {_bot_status['minutes_ago']} minutes ago.")

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
_notif_rows = db.get_notifications(
    severity_in=["critical", "warning"],
    limit=_MAX_ALERTS,
    since=st.session_state.dismissed_before,
)
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

_max_exposure_pct = WEATHER.get("decision_max_exposure_pct", 0.80)
_exposure_cap = account_value * _max_exposure_pct
_capital_available = max(0.0, min(cash, _exposure_cap - position_val))

c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
with c1:
    st.metric("Account Balance", f"${account_value:,.2f}", delta=f"{acct_pnl_pct:+.1f}%")
with c2:
    st.metric("Trading Capital (Floor)", f"${_capital_available:,.2f}",
              delta=f"cap {_max_exposure_pct:.0%} exp", delta_color="off")
with c3:
    st.metric("Untraded Balance", f"${cash:,.2f}")
with c4:
    st.metric("Realized P&L", f"${realized_pnl:+.2f}", delta=f"{realized_pct:+.1f}%")
with c5:
    st.metric("Unrealized P&L", f"${unrealized:+.2f}")
with c6:
    st.metric("Win Rate", f"{stats['win_rate']:.1f}%",
              delta=f"{stats['wins']}W / {stats['losses']}L", delta_color="off")
with c7:
    st.metric("Open Positions", len(open_trades))
with c8:
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

# ── Open positions ────────────────────────────────────────────────────────────

_c1, _c2 = st.columns([6, 1])
with _c1:
    st.subheader(f"Open Positions ({len(open_trades)})")
with _c2:
    if "op_table_reset" not in st.session_state:
        st.session_state["op_table_reset"] = 0
    if st.button("↻ Reset sort", key="btn_reset_op"):
        st.session_state["op_table_reset"] += 1
        st.rerun()

if open_trades:
    # Group extended positions: parents with aggregated legs, standalone unchanged
    _op_parent_map = {}   # parent_id → list of child legs
    _op_standalone = []
    _op_child_ids = {t["id"] for t in open_trades if t.get("parent_trade_id") is not None}
    _op_parent_ids = {t.get("parent_trade_id") for t in open_trades if t.get("parent_trade_id") is not None}

    for t in open_trades:
        pid = t.get("parent_trade_id")
        if pid is not None:
            _op_parent_map.setdefault(pid, []).append(t)
        elif t["id"] in _op_parent_ids:
            _op_parent_map.setdefault(t["id"], [])
        else:
            _op_standalone.append(t)

    # Build aggregates for parent positions
    _op_aggregates = {}  # parent_id → aggregate dict

    for pid, children in _op_parent_map.items():
        parent = next((t for t in open_trades if t["id"] == pid), None)
        if parent is None:
            continue
        all_legs = [parent] + children
        total_size = sum(l["size_usdc"] for l in all_legs)
        total_shares = sum(l.get("shares", 0) for l in all_legs)
        weighted_fill = sum(l["fill_price"] * l["size_usdc"] for l in all_legs) / total_size if total_size else 0
        total_unreal = sum(_unreal_pnl(l) for l in all_legs)
        leg_count = len(all_legs)
        max_legs = WEATHER.get("extended_positions_max_add_ons", 2) + 1

        agg = dict(parent)
        agg["size_usdc"] = total_size
        agg["shares"] = total_shares
        agg["fill_price"] = weighted_fill
        agg["_unreal_override"] = total_unreal
        agg["_leg_display"] = f"{leg_count}/{max_legs} legs"
        agg["_legs"] = all_legs
        _op_aggregates[pid] = agg

    # Build display list in original query order (opened_at DESC),
    # placing aggregates where their parent naturally appears
    _op_display = []
    for t in open_trades:
        pid = t.get("parent_trade_id")
        if pid is not None:
            continue  # child leg — rendered inline under its parent
        if t["id"] in _op_aggregates:
            _op_display.append(_op_aggregates[t["id"]])
        else:
            _op_display.append(t)

    rows = []
    for t in _op_display:
        h = _hours_left(t.get("end_date", ""))
        unreal = t.get("_unreal_override", _unreal_pnl(t))
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

        # _pid used to match parent rows to their leg sub-rows; dropped before display
        row = {
            "_pid":          t["id"] if t["id"] in _op_aggregates else None,
            "City":         (t.get("city") or "—").title(),
            "Threshold":    _parse_threshold(t),
            "Bet":          t["direction"].title(),
            "Legs":         t.get("_leg_display", "—"),
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
        }
        rows.append(row)

    # Insert leg sub-rows inline beneath their parent
    expanded_rows = []
    for r in rows:
        expanded_rows.append(r)
        pid = r.get("_pid")
        if pid and pid in _op_aggregates:
            for leg in sorted(_op_aggregates[pid]["_legs"], key=lambda l: l.get("leg_number") or 1):
                num = leg.get("leg_number") or 1
                label = "Entry" if num == 1 else f"Leg {num - 1}"
                leg_unreal = _unreal_pnl(leg)
                expanded_rows.append({
                    "_pid":         None,
                    "City":         f"  ↳ {label}",
                    "Threshold":    "",
                    "Bet":          "",
                    "Legs":         "",
                    "Tier":         _edge_tier(leg.get("edge_score")),
                    "Edge %":       round(leg.get("edge_score") or 0, 1),
                    "Model %":      round((leg.get("estimated_prob") or 0) * 100, 1),
                    "Mkt %":        round((leg.get("entry_price") or 0) * 100, 1),
                    "Ens. Entry":   f"{int(leg.get('entry_ensemble_yes', 0))}/{leg.get('entry_ensemble_n', 0)}"
                                    if leg.get("entry_ensemble_n") else "—",
                    "Ens. Current": "",
                    "Tr Ens":       "",
                    "Fill":         round(leg["fill_price"], 3),
                    "Current":      round(leg.get("current_price") or leg["fill_price"], 3),
                    "Unreal. P&L":  round(leg_unreal, 2),
                    "Unreal. P&L %": round((leg_unreal / leg["size_usdc"]) * 100, 1) if leg["size_usdc"] else None,
                    "Size $":       round(leg["size_usdc"], 2),
                    "Vol 24h":      round(leg["volume_24h"]) if leg.get("volume_24h") else None,
                    "Closes":       _fmt_hours(_hours_left(leg.get("end_date", ""))),
                    "Market":       "",
                    "Opened":       (leg.get("opened_at") or "")[:16],
                })

    df_open = pd.DataFrame(expanded_rows).drop(columns=["_pid"])
    col_cfg = {
        "Edge %":      st.column_config.NumberColumn(format="%.1f%%"),
        "Model %":     st.column_config.NumberColumn(format="%.1f%%"),
        "Mkt %":       st.column_config.NumberColumn(format="%.1f%%"),
        "Size $":      st.column_config.NumberColumn(format="$%.2f"),
        "Unreal. P&L":   st.column_config.NumberColumn(format="$%.2f"),
        "Unreal. P&L %": st.column_config.NumberColumn(format="%.1f%%"),
        "Vol 24h":       st.column_config.NumberColumn(format="$%d"),
    }
    if any(r["Market"].startswith("http") for r in expanded_rows if r["Market"]):
        col_cfg["Market"] = st.column_config.LinkColumn(
            "Market", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_open, column_config=col_cfg, width='stretch', hide_index=True,
                 key=f"df_open_{st.session_state.get('op_table_reset', 0)}")

    # Per-trade close buttons — horizontal row of buttons, confirmation below
    st.caption("Manual close:")
    _btn_cols = st.columns(min(len(_op_display), 4))
    _pending_close = None
    for i, t in enumerate(_op_display):
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

        if st.session_state[state_key]:
            elapsed = time.time() - (st.session_state[time_key] or 0)
            if elapsed > 10:
                st.session_state[state_key] = False

        with _btn_cols[i % 4]:
            n_legs = len(t["_legs"]) if t.get("_legs") else 0
            label = f"Close: {city} {direction} {threshold}" + (f" ({n_legs} legs)" if n_legs > 1 else "")
            btn_type = "primary" if st.session_state[state_key] else "secondary"
            if st.button(label, key=f"btn_close_{trade_id}", type=btn_type):
                st.session_state[state_key] = not st.session_state[state_key]
                st.session_state[time_key] = time.time() if st.session_state[state_key] else None
                st.rerun()

        if st.session_state[state_key]:
            _pending_close = (t, trade_id, city, direction, threshold, state_key)

    if _pending_close:
        t, trade_id, city, direction, threshold, state_key = _pending_close
        n_legs = len(t["_legs"]) if t.get("_legs") else 0
        close_msg = f"Confirm close: **{city} {direction} {threshold}**"
        if n_legs > 1:
            close_msg += f" (all {n_legs} legs)"
        close_msg += "? (auto-cancels in 10s)"
        st.warning(close_msg)
        c1, c2, _ = st.columns([1, 1, 6])
        with c1:
            if st.button("✅ Confirm", key=f"btn_confirm_{trade_id}", type="primary"):
                executor = _get_executor()
                if executor is None:
                    st.error("Cannot close — executor not available. Check wallet config.")
                else:
                    if t.get("_legs"):
                        executor.close_position(t, reason="manual_close")
                    else:
                        executor.close_full(t, reason="manual_close")
                    _risk_mgr.add_manual_close(t.get("market_id", ""))
                    print(f"[risk] manual close: {t.get('market_name', '')[:50]}")
                    st.session_state[state_key] = False
                    st.toast(f"Closed {city} {direction} {threshold}.")
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

_c3, _c4 = st.columns([6, 1])
with _c3:
    st.subheader(f"Trade History ({len(closed_trades)} closed)")
with _c4:
    if "cl_table_reset" not in st.session_state:
        st.session_state["cl_table_reset"] = 0
    if st.button("↻ Reset sort", key="btn_reset_cl"):
        st.session_state["cl_table_reset"] += 1
        st.rerun()

if closed_trades:
    # Group extended positions in closed trades
    _cl_parent_map = {}
    _cl_standalone = []
    _cl_parent_ids = {t.get("parent_trade_id") for t in closed_trades if t.get("parent_trade_id") is not None}

    for t in closed_trades:
        pid = t.get("parent_trade_id")
        if pid is not None:
            _cl_parent_map.setdefault(pid, []).append(t)
        elif t["id"] in _cl_parent_ids:
            _cl_parent_map.setdefault(t["id"], [])
        else:
            _cl_standalone.append(t)

    _cl_aggregates = {}

    for pid, children in _cl_parent_map.items():
        parent = next((t for t in closed_trades if t["id"] == pid), None)
        if parent is None:
            continue
        all_legs = [parent] + children
        total_size = sum(l["size_usdc"] for l in all_legs)
        total_pnl = sum(l.get("pnl") or 0 for l in all_legs)
        weighted_fill = sum(l["fill_price"] * l["size_usdc"] for l in all_legs) / total_size if total_size else 0
        exit_prices = [l.get("exit_price") for l in all_legs if l.get("exit_price") is not None]
        weighted_exit = sum((l.get("exit_price") or 0) * l["size_usdc"] for l in all_legs) / total_size if total_size and exit_prices else 0
        leg_count = len(all_legs)

        agg = dict(parent)
        agg["size_usdc"] = total_size
        agg["fill_price"] = weighted_fill
        agg["exit_price"] = weighted_exit
        agg["pnl"] = total_pnl
        agg["pnl_pct"] = (total_pnl / total_size * 100) if total_size else 0
        agg["_leg_display"] = f"{leg_count} legs"
        agg["_legs"] = all_legs
        _cl_aggregates[pid] = agg

    # Build display list in original query order,
    # placing aggregates where their parent naturally appears
    _cl_display = []
    for t in closed_trades:
        pid = t.get("parent_trade_id")
        if pid is not None:
            continue  # child leg — rendered inline under its parent
        if t["id"] in _cl_aggregates:
            _cl_display.append(_cl_aggregates[t["id"]])
        else:
            _cl_display.append(t)

    rows = []
    for t in _cl_display:
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
            "_pid":         t["id"] if t["id"] in _cl_aggregates else None,
            "City":         (t.get("city") or "—").title(),
            "Threshold":    _parse_threshold(t),
            "Bet":          t["direction"].title(),
            "Legs":         t.get("_leg_display", "—"),
            "Tier":         _edge_tier(t.get("edge_score")),
            "Edge %":       round(t.get("edge_score") or 0, 1),
            "Model %":      round((t.get("estimated_prob") or 0) * 100, 1),
            "Mkt %":        round((t.get("entry_price") or 0) * 100, 1),
            "Ens. Entry":   ens_str,
            "Ens. Exit":    f"{int(t['current_ensemble_yes'])}/{int(t['current_ensemble_n'])}"
                            if t.get("current_ensemble_yes") is not None and t.get("current_ensemble_n") else "—",
            "Tr Ens":       _tr_ens(t.get("market_id", ""), t.get("direction", "")),
            "Fill":         round(t["fill_price"], 3),
            "Exit":         round(t.get("exit_price") or 0, 3),
            "P&L $":        round(t.get("pnl") or 0, 2),
            "P&L %":        round(t.get("pnl_pct") or 0, 1),
            "Size $":       round(t["size_usdc"], 2),
            "Hrs @ Entry":  _fmt_hours(t.get("hours_to_close_at_entry")),
            "Hrs @ Exit":   _fmt_hours(t.get("hours_to_close_at_exit")),
            "Vol 24h":      round(t["volume_24h"]) if t.get("volume_24h") else None,
            "Exit Reason":  (
                f"{(t.get('exit_reason') or 'resolved')[:28]} (claim pending)"
                if t["status"] == "claim_pending"
                else (t.get("exit_reason") or "resolved")[:40]
            ),
            "Market":       url or t.get("market_name", ""),
            "Opened":       (t.get("opened_at") or "")[:16],
            "Closed":       (t.get("closed_at") or "")[:16],
        })

    # Insert leg sub-rows inline beneath their parent
    expanded_rows = []
    for r in rows:
        expanded_rows.append(r)
        pid = r.get("_pid")
        if pid and pid in _cl_aggregates:
            for leg in sorted(_cl_aggregates[pid]["_legs"], key=lambda l: l.get("leg_number") or 1):
                num = leg.get("leg_number") or 1
                label = "Entry" if num == 1 else f"Leg {num - 1}"
                expanded_rows.append({
                    "_pid":         None,
                    "City":         f"  ↳ {label}",
                    "Threshold":    "",
                    "Bet":          "",
                    "Legs":         "",
                    "Tier":         _edge_tier(leg.get("edge_score")),
                    "Edge %":       round(leg.get("edge_score") or 0, 1),
                    "Model %":      round((leg.get("estimated_prob") or 0) * 100, 1),
                    "Mkt %":        round((leg.get("entry_price") or 0) * 100, 1),
                    "Ens. Entry":   f"{int(leg.get('entry_ensemble_yes', 0))}/{leg.get('entry_ensemble_n', 0)}"
                                    if leg.get("entry_ensemble_n") else "—",
                    "Ens. Exit":    f"{int(leg['current_ensemble_yes'])}/{int(leg['current_ensemble_n'])}"
                                    if leg.get("current_ensemble_yes") is not None and leg.get("current_ensemble_n") else "—",
                    "Tr Ens":       "",
                    "Fill":         round(leg["fill_price"], 3),
                    "Exit":         round(leg.get("exit_price") or 0, 3),
                    "P&L $":        round(leg.get("pnl") or 0, 2),
                    "P&L %":        round((leg.get("pnl") or 0) / leg["size_usdc"] * 100, 1) if leg["size_usdc"] else None,
                    "Size $":       round(leg["size_usdc"], 2),
                    "Hrs @ Entry":  _fmt_hours(leg.get("hours_to_close_at_entry")),
                    "Hrs @ Exit":   _fmt_hours(leg.get("hours_to_close_at_exit")),
                    "Vol 24h":      round(leg["volume_24h"]) if leg.get("volume_24h") else None,
                    "Exit Reason":  (leg.get("exit_reason") or "")[:40],
                    "Market":       "",
                    "Opened":       (leg.get("opened_at") or "")[:16],
                    "Closed":       (leg.get("closed_at") or "")[:16],
                })

    df_closed = pd.DataFrame(expanded_rows).drop(columns=["_pid"])
    closed_col_cfg = {
        "Edge %":  st.column_config.NumberColumn(format="%.1f%%"),
        "Model %": st.column_config.NumberColumn(format="%.1f%%"),
        "Mkt %":   st.column_config.NumberColumn(format="%.1f%%"),
        "P&L $":   st.column_config.NumberColumn(format="$%.2f"),
        "P&L %":   st.column_config.NumberColumn(format="%.1f%%"),
        "Size $":  st.column_config.NumberColumn(format="$%.2f"),
        "Vol 24h": st.column_config.NumberColumn(format="$%d"),
    }
    if any(r["Market"].startswith("http") for r in expanded_rows if r["Market"]):
        closed_col_cfg["Market"] = st.column_config.LinkColumn(
            "Market", display_text=r"https://polymarket\.com/event/([^/]+)",
        )
    st.dataframe(df_closed, column_config=closed_col_cfg, width='stretch', hide_index=True,
                 key=f"df_closed_{st.session_state.get('cl_table_reset', 0)}")

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

    # ── Analytics charts ─────────────────────────────────────────────────────
    if len(closed_trades) > 1:

        # Chart 1: P&L by City
        st.markdown("**P&L by City**")
        city_pnl: dict   = defaultdict(float)
        city_count: dict = defaultdict(int)
        for t in closed_trades:
            city = (t.get("city") or "Unknown").title()
            city_pnl[city]   += t.get("pnl") or 0
            city_count[city] += 1
        cities      = sorted(city_pnl.keys())
        city_y      = [city_pnl[c] for c in cities]
        city_colors = ["#00cc88" if v >= 0 else "#ff4444" for v in city_y]
        city_hover  = [
            f"{c}<br>Trades: {city_count[c]}<br>Net P&L: ${city_pnl[c]:.2f}"
            for c in cities
        ]
        fig_city = go.Figure(go.Bar(
            x=cities, y=city_y,
            marker_color=city_colors,
            hovertext=city_hover, hoverinfo="text",
        ))
        fig_city.update_layout(
            height=240, margin=dict(l=0, r=0, t=10, b=0),
            yaxis_tickprefix="$",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False), yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
            showlegend=False,
        )
        st.plotly_chart(fig_city, width='stretch')

        # Charts 2 & 3: Edge scatter | Direction P&L
        col_scatter, col_dir = st.columns(2)

        # Chart 2: Edge % vs P&L scatter
        with col_scatter:
            st.markdown("**Edge % vs P&L**")
            edge_x    = [t.get("edge_score") or 0 for t in closed_trades]
            pnl_y     = [t.get("pnl") or 0 for t in closed_trades]
            sc_dirs   = [t.get("direction", "YES") for t in closed_trades]
            sc_colors = ["#00cc88" if d == "YES" else "#ff9900" for d in sc_dirs]
            sc_hover  = [
                f"{(t.get('city') or '?').title()}<br>"
                f"Direction: {t.get('direction')}<br>"
                f"Edge: {t.get('edge_score') or 0:.1f}%<br>"
                f"P&L: ${t.get('pnl') or 0:.2f}"
                for t in closed_trades
            ]
            fig_scatter = go.Figure(go.Scatter(
                x=edge_x, y=pnl_y,
                mode="markers",
                marker=dict(color=sc_colors, size=9, opacity=0.85),
                hovertext=sc_hover, hoverinfo="text",
            ))
            fig_scatter.add_hline(y=0, line_dash="dash", line_color="gray", line_width=1)
            fig_scatter.update_layout(
                height=260, margin=dict(l=0, r=0, t=10, b=0),
                xaxis_title="Edge %", yaxis_tickprefix="$",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
                showlegend=False,
            )
            st.plotly_chart(fig_scatter, width='stretch')

        # Chart 3: Net P&L by Direction
        with col_dir:
            st.markdown("**Net P&L by Direction**")
            yes_trades = [t for t in closed_trades if t.get("direction") == "YES"]
            no_trades  = [t for t in closed_trades if t.get("direction") == "NO"]
            yes_pnl    = sum(t.get("pnl") or 0 for t in yes_trades)
            no_pnl     = sum(t.get("pnl") or 0 for t in no_trades)
            yes_wins   = sum(1 for t in yes_trades if (t.get("pnl") or 0) > 0)
            no_wins    = sum(1 for t in no_trades  if (t.get("pnl") or 0) > 0)
            dir_hover  = [
                f"YES<br>Trades: {len(yes_trades)}<br>Net P&L: ${yes_pnl:.2f}"
                + (f"<br>Win Rate: {yes_wins/len(yes_trades)*100:.0f}%" if yes_trades else ""),
                f"NO<br>Trades: {len(no_trades)}<br>Net P&L: ${no_pnl:.2f}"
                + (f"<br>Win Rate: {no_wins/len(no_trades)*100:.0f}%" if no_trades else ""),
            ]
            fig_dir = go.Figure(go.Bar(
                x=["YES", "NO"], y=[yes_pnl, no_pnl],
                marker_color=[
                    "#00cc88" if yes_pnl >= 0 else "#ff4444",
                    "#00cc88" if no_pnl  >= 0 else "#ff4444",
                ],
                hovertext=dir_hover, hoverinfo="text",
            ))
            fig_dir.update_layout(
                height=260, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_tickprefix="$",
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False), yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
                showlegend=False,
            )
            st.plotly_chart(fig_dir, width='stretch')

        # Chart 4: Rolling Win Rate (last 10 trades)
        if len(closed_trades) >= 3:
            st.markdown("**Rolling Win Rate (last 10 trades)**")
            WINDOW     = 10
            sorted_ct  = sorted(closed_trades, key=lambda t: t.get("closed_at") or "")
            rolling_wr = []
            for i in range(len(sorted_ct)):
                window = sorted_ct[max(0, i - WINDOW + 1): i + 1]
                wins   = sum(1 for t in window if (t.get("pnl") or 0) > 0)
                rolling_wr.append(wins / len(window) * 100)
            trade_nums = list(range(1, len(sorted_ct) + 1))
            fig_roll = go.Figure()
            fig_roll.add_trace(go.Scatter(
                x=trade_nums, y=rolling_wr,
                mode="lines+markers",
                line=dict(color="#00cc88", width=2),
                marker=dict(size=5),
                hovertemplate="Trade #%{x}<br>Rolling Win Rate: %{y:.0f}%<extra></extra>",
            ))
            fig_roll.add_hline(
                y=50, line_dash="dash", line_color="gray", line_width=1,
                annotation_text="50%", annotation_position="right",
            )
            fig_roll.update_layout(
                height=220, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(
                    ticksuffix="%", range=[0, 100],
                    gridcolor="rgba(255,255,255,0.08)",
                ),
                xaxis=dict(title="Trade #", showgrid=False),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False,
            )
            st.plotly_chart(fig_roll, width='stretch')

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
