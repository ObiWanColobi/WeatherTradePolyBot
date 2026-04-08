# P&L Analytics Charts — Design Spec

**Date:** 2026-04-08
**File:** `ui/weather_dashboard.py`

## Problem

The existing "P&L per Trade" bar chart uses `City Threshold Direction` as x-axis labels. With 25+ trades these labels overlap into an unreadable mess even when angled.

## Solution

Replace the single per-trade bar chart with four analytics charts arranged in a 2×2 grid below the Performance Breakdown tables.

---

## Charts

### 1. P&L by City (top-left, full-width or left column)
- **Type:** Horizontal or vertical bar chart
- **X-axis:** City name (e.g. "Wellington", "London")
- **Y-axis:** Net P&L in dollars (sum of all closed trades for that city)
- **Color:** Green (#00cc88) if net positive, red (#ff4444) if net negative
- **Hover:** City name, trade count, net P&L $
- **Replaces** the existing per-trade bar chart entirely

### 2. Edge vs P&L Scatter (top-right)
- **Type:** Scatter plot
- **X-axis:** Entry edge score (edge_score field, %)
- **Y-axis:** P&L in dollars (pnl field)
- **Color:** Green for YES trades, blue/orange for NO trades
- **Hover:** City, direction, edge %, P&L $
- **Purpose:** Validates whether higher-edge trades are actually more profitable

### 3. P&L by Direction (bottom-left)
- **Type:** Bar chart, two bars only (YES / NO)
- **Y-axis:** Net P&L in dollars
- **Color:** Green/red per bar sign
- **Hover:** Direction, trade count, net P&L $, win rate %
- **Note:** Complements the existing text table with a visual; keep both

### 4. Rolling Win Rate (bottom-right)
- **Type:** Line chart
- **X-axis:** Trade number (chronological order by closed_at)
- **Y-axis:** Win rate % (rolling window of last 10 trades)
- **Reference line:** Dashed line at 50%
- **Hover:** Trade #, rolling win rate %
- **Purpose:** Detect model degradation over time
- **Window size:** 10 trades (hardcoded, enough signal without too much lag)

---

## Layout

```
[ Chart 1: P&L by City (full width) ]
[ Chart 2: Edge Scatter | Chart 3: Direction P&L ]
[ Chart 4: Rolling Win Rate (full width) ]
```

Charts 2 and 3 share a row via `st.columns(2)`.

---

## Implementation Scope

All changes are confined to `ui/weather_dashboard.py`. The target section is lines 647–668 (the existing `# P&L chart` block). Replace that block with the four new charts.

Data already available on each closed trade:
- `city`, `direction`, `edge_score`, `pnl`, `pnl_pct`, `closed_at`

No DB changes needed. All charts are computed from the `closed_trades` list already loaded.

---

## Out of Scope

- No new DB queries
- No new helper functions in other files
- Rolling window size is not user-configurable (keep it simple)
