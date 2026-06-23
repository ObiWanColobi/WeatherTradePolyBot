"""Load snapshot parquet into (city, resolution-date) units with truth + time split."""
from __future__ import annotations
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

_RDB = Path(__file__).resolve().parents[2] / "research_db"
sys.path.insert(0, str(_RDB))
import snapshot_replay  # noqa: E402  (fetch_snapshots, _fetch_snapshots_parquet)


@dataclass
class CityDay:
    city: str
    resolution_date: str
    kind: str
    snapshots: "pd.DataFrame | None"
    truth_f: "float | None"


def time_split(city_days: list[CityDay], holdout_frac: float = 0.34):
    """Split by DATE: latest `holdout_frac` of distinct resolution_dates -> holdout."""
    dates = sorted({cd.resolution_date for cd in city_days})
    if not dates:
        return [], []
    n_holdout = max(1, round(len(dates) * holdout_frac))
    holdout_dates = set(dates[-n_holdout:])
    tune = [cd for cd in city_days if cd.resolution_date not in holdout_dates]
    holdout = [cd for cd in city_days if cd.resolution_date in holdout_dates]
    return tune, holdout


def load_city_days(parquet_dir: str, kind: str = "highest") -> list[CityDay]:
    """Group snapshot rows by (city, resolution_date); attach METAR truth_f.

    Reuses snapshot_replay's parquet reader, which only ever returns highest-kind
    rows (its SQL hard-codes kind='highest'). Daily-low markets are not supported.
    """
    if kind != "highest":
        raise NotImplementedError(
            f"load_city_days only supports kind='highest'; the upstream parquet reader "
            f"(_fetch_snapshots_parquet) hard-codes kind='highest'. Daily-low markets "
            f"(kind={kind!r}) need a dedicated reader — deferred to a later phase."
        )
    df = snapshot_replay._fetch_snapshots_parquet(Path(parquet_dir))
    if "kind" in df.columns:
        df = df[df["kind"] == kind]
    truth = _load_truth()  # {(city, date): daily_max_f}
    out: list[CityDay] = []
    for (city, res_date), grp in df.groupby(["city", "resolution_date"]):
        res_date_key = str(res_date)[:10]  # canonical bare date, matches _load_truth keys
        out.append(CityDay(
            city=str(city),
            resolution_date=res_date_key,
            kind=kind,
            snapshots=grp.reset_index(drop=True),
            truth_f=truth.get((str(city), res_date_key)),
        ))
    return out


def _load_truth() -> dict[tuple[str, str], float]:
    """Per (city, city-local-date) METAR daily max in °F (raw float; rounding happens
    at settlement inside shotgun.bets.winset_resolved)."""
    import duckdb
    import importlib
    sys.path.insert(0, str(_RDB))
    bt = importlib.import_module("51_snapshot_backtest")
    with duckdb.connect(str(bt.RESEARCH_DB), read_only=True) as con:
        tdf = bt.fetch_resolution_truth(con)
    # Normalize the date key to bare 'YYYY-MM-DD'. The truth query returns a timestamp
    # ('2026-01-28 00:00:00') but the snapshot loader's resolution_date is a bare date;
    # without this they never join and every trade would settle "unresolved" (silently
    # zeroing P&L). Canonicalize to the first 10 chars on both sides of the join.
    return {
        (str(r.city), str(r.resolution_date)[:10]): float(r.daily_max_f)
        for r in tdf.itertuples(index=False)
    }
