# P&L Analytics Charts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unreadable per-trade P&L bar chart with 4 clean analytics charts in `ui/weather_dashboard.py`.

**Architecture:** All changes are confined to a single block in `ui/weather_dashboard.py` (lines 647–668, the `# P&L chart` section). The existing `closed_trades` list provides all needed data — no DB or helper changes required.

**Tech Stack:** Python, Streamlit, Plotly (`plotly.graph_objects` already imported as `go`)

---

## File Map

- **Modify:** `ui/weather_dashboard.py` — replace the `# P&L chart` block (lines 647–668) with 4 new chart blocks

---

### Task 1: Replace per-trade chart with P&L by City

**Files:**
- Modify: `ui/weather_dashboard.py:647-668`

- [ ] **Step 1: Delete the existing P&L chart block and replace with Chart 1**

Find and replace this entire block (lines 647–668):

```python
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
```

With this:

```python
    # ── Analytics charts ─────────────────────────────────────────────────────
    if len(closed_trades) > 1:

        # Chart 1: P&L by City
        st.markdown("**P&L by City**")
        from collections import defaultdict
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
```

- [ ] **Step 2: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(ui): replace per-trade P&L chart with P&L by city"
```

---

### Task 2: Add Edge vs P&L scatter and Direction P&L side by side

**Files:**
- Modify: `ui/weather_dashboard.py` — append after the city chart block, still inside `if len(closed_trades) > 1:`

- [ ] **Step 1: Add the two-column row with scatter and direction charts**

Append immediately after `st.plotly_chart(fig_city, width='stretch')`, still inside the `if len(closed_trades) > 1:` block:

```python
        # Charts 2 & 3: Edge scatter | Direction P&L
        col_scatter, col_dir = st.columns(2)

        # Chart 2: Edge % vs P&L scatter
        with col_scatter:
            st.markdown("**Edge % vs P&L**")
            edge_x      = [t.get("edge_score") or 0 for t in closed_trades]
            pnl_y       = [t.get("pnl") or 0 for t in closed_trades]
            sc_dirs     = [t.get("direction", "YES") for t in closed_trades]
            sc_colors   = ["#00cc88" if d == "YES" else "#ff9900" for d in sc_dirs]
            sc_hover    = [
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
```

- [ ] **Step 2: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(ui): add edge scatter and direction P&L charts"
```

---

### Task 3: Add Rolling Win Rate chart

**Files:**
- Modify: `ui/weather_dashboard.py` — append after the two-column block, still inside `if len(closed_trades) > 1:`

- [ ] **Step 1: Add rolling win rate chart**

Append immediately after the `col_dir` block closes, still inside `if len(closed_trades) > 1:`:

```python
        # Chart 4: Rolling Win Rate (last 10 trades)
        if len(closed_trades) >= 3:
            st.markdown("**Rolling Win Rate (last 10 trades)**")
            WINDOW       = 10
            sorted_ct    = sorted(closed_trades, key=lambda t: t.get("closed_at") or "")
            rolling_wr   = []
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
```

- [ ] **Step 2: Commit**

```bash
git add ui/weather_dashboard.py
git commit -m "feat(ui): add rolling win rate chart"
```

---

## Self-Review

- Spec coverage: All 4 charts covered. Layout matches spec (city full-width, scatter+direction side-by-side, rolling win rate full-width). ✓
- No placeholders. ✓
- `defaultdict` import is inside the `if` block — fine for Streamlit but should be moved to top of file if it's not already imported. Check: `from collections import defaultdict` should be at the top of `weather_dashboard.py` alongside other imports.
- `closed_trades` is already available in scope at this point in the file. ✓
- `go` is already imported as `plotly.graph_objects`. ✓

**Pre-flight check:** Before Task 1, verify `from collections import defaultdict` exists at the top of `weather_dashboard.py`. If not, add it there rather than inside the chart block.
