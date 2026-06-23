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

    Reuses snapshot_replay's parquet reader. NOTE: snapshot_replay.fetch_snapshots
    filters to highest-kind; for kind='lowest' we read the parquet directly via
    _fetch_snapshots_parquet and filter on the `kind` column ourselves.
    """
    import os
    os.environ["SNAPSHOT_PARQUET_DIR"] = parquet_dir
    df = snapshot_replay._fetch_snapshots_parquet(Path(parquet_dir))
    if "kind" in df.columns:
        df = df[df["kind"] == kind]
    truth = _load_truth()  # {(city, date): daily_max_f}
    out: list[CityDay] = []
    for (city, res_date), grp in df.groupby(["city", "resolution_date"]):
        out.append(CityDay(
            city=str(city),
            resolution_date=str(res_date),
            kind=kind,
            snapshots=grp.reset_index(drop=True),
            truth_f=truth.get((str(city), str(res_date))),
        ))
    return out


def _load_truth() -> dict[tuple[str, str], float]:
    """Per (city, city-local-date) integer-rounded METAR daily max in °F."""
    import duckdb
    con = duckdb.connect()
    sys.path.insert(0, str(_RDB))
    import importlib
    bt = importlib.import_module("51_snapshot_backtest")
    tdf = bt.fetch_resolution_truth(con)
    return {
        (str(r.city), str(r.resolution_date)): float(r.daily_max_f)
        for r in tdf.itertuples(index=False)
    }
