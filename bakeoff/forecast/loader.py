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


def _to_naive_utc(ts):
    """Coerce a timestamp-like value to a tz-naive UTC pandas Timestamp (NaT-safe)."""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def members_asof(gefs_df: pd.DataFrame, canon_city_name: str,
                 resolution_date: str, decision_ts: str) -> list[float]:
    """Latest init at-or-before decision_ts for this city-day; its member temps (°F).

    Compares dates/timestamps as REAL temporal values, not strings. A lexical string
    compare ('2026-03-31 06:00:00' <= '2026-03-31T03:00:00Z' is True because ' ' < 'T')
    would silently admit a future init = lookahead, the cardinal backtest sin. This
    mirrors the production selection in snapshot_replay.py (coerce + normalize TZ).
    """
    init_norm = gefs_df["init_ts"].map(_to_naive_utc)
    local_date_d = pd.to_datetime(gefs_df["local_date"]).dt.normalize()
    res_d = pd.Timestamp(resolution_date).normalize()
    dec = _to_naive_utc(decision_ts)
    mask = (
        (gefs_df["city"] == canon_city_name)
        & (local_date_d == res_d)
        & (init_norm <= dec)
    )
    sub = gefs_df[mask]
    if sub.empty:
        return []
    best_init = init_norm[mask].max()
    members = sub[init_norm[mask] == best_init]
    return members["daily_max_f"].dropna().tolist()
