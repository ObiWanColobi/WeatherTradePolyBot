"""Pick the fire-time snapshot per city-day: one priceable row per bucket."""
from __future__ import annotations
import pandas as pd

_FIELDS = ("bound_lo_f", "bound_hi_f", "is_open_tail", "best_ask", "best_bid",
           "group_item_title", "orderbook_bids_json", "orderbook_asks_json")


def decision_rows(snapshots: pd.DataFrame, hours_before_close: int = 12):
    if snapshots is None or len(snapshots) == 0:
        return "", []
    df = snapshots.copy()
    df["_ts"] = pd.to_datetime(df["snapshot_at_utc"], errors="coerce")
    df = df.dropna(subset=["_ts"])
    if df.empty:
        return "", []
    # Target = close (latest snapshot) minus the fire window; pick the nearest available time.
    close = df["_ts"].max()
    target = close - pd.Timedelta(hours=hours_before_close)
    nearest_ts = df["_ts"].iloc[(df["_ts"] - target).abs().argsort().iloc[0]]
    at = df[df["_ts"] == nearest_ts]
    rows = []
    for _, g in at.groupby("sub_market_condition_id"):
        r = g.iloc[0]
        rows.append({f: r[f] for f in _FIELDS} | {"sub_market_condition_id": r["sub_market_condition_id"]})
    return str(nearest_ts), rows
