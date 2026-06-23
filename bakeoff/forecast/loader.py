"""Point-in-time historical ensemble members per gradeable city-day (backtest)."""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

_RDB = Path(__file__).resolve().parents[2] / "research_db"
sys.path.insert(0, str(_RDB))
import snapshot_replay  # _agg_gefs_member_dailymax
import importlib
_bt = importlib.import_module("51_snapshot_backtest")


def canon_city(city: str) -> str:
    """Polymarket slug -> research-DB canonical (lowercase, spaces)."""
    return city.lower().replace("-", " ")


def load_ensemble_members(cities: list[str]) -> pd.DataFrame:
    """Per (city, init_ts, model, local_date) daily-max °F for the canonical city list."""
    import duckdb
    canon = sorted({canon_city(c) for c in cities})
    with duckdb.connect(str(_bt.RESEARCH_DB), read_only=True) as con:
        return snapshot_replay._agg_gefs_member_dailymax(con, canon)


def members_asof(gefs_df: pd.DataFrame, canon_city_name: str,
                 resolution_date: str, decision_ts: str) -> list[float]:
    """Latest init at-or-before decision_ts for this city-day; its member temps (°F)."""
    sub = gefs_df[
        (gefs_df["city"] == canon_city_name)
        & (gefs_df["local_date"].astype(str) == str(resolution_date))
        & (gefs_df["init_ts"].astype(str) <= str(decision_ts))
    ]
    if sub.empty:
        return []
    best_init = sub["init_ts"].max()
    members = sub[sub["init_ts"] == best_init]
    return members["daily_max_f"].dropna().tolist()
