# bakeoff/run_nowcast_track.py
"""Runner for candidate #8: intraday conditional-max nowcast.

Distinct from the microstructure runner in two ways:
  1. Fire time is chosen by LOCAL CLOCK HOUR (default ~12:00 local), not hours-before-close.
     The 12h-before-close snapshot lands ~3-7am local for these markets (pre-dawn: the daily
     max hasn't begun building, so the running-max signal is empty). The nowcast thesis needs
     a MIDDAY fire, after morning warming is observed but before the peak locks.
  2. It reconstructs, lookahead-safely, the running-max temperature at fire time from
     weather_obs, and learns the remaining-warming distribution from history STRICTLY BEFORE
     each scored day (no leakage), so it can hand the candidate a real observed-temperature
     signal.

Honesty invariants (see candidate docstring for the full list):
  - running_max_so_far(fire) uses only obs with observed_at <= fire_ts, and the SAME
    source/obs_type as settlement truth (historical_archive / metar_hourly) so it converges to
    the settled daily max at end of day.
  - the remaining-warming model for a scored resolution_date D is trained only on obs from
    local-days strictly before D (leak-free), pooled across cities, bucketed by local hour.
  - depth window / truth gating identical to the microstructure runner.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_RDB = Path(__file__).resolve().parents[1] / "research_db"
sys.path.insert(0, str(_RDB))

import pandas as pd  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from bakeoff.harness.loader import load_city_days, CityDay  # noqa: E402
from bakeoff.harness.scorer import score  # noqa: E402
from bakeoff.candidates import c8_intraday_nowcast as c8  # noqa: E402

_r11 = __import__("11_load_resolutions")
CITY_TZ = _r11.CITY_TZ

PARQUET_DIR = "research_db/snapshot_parquet_rebuild"
DEPTH_WINDOW_MIN = "2026-06-26"
DEPTH_WINDOW_MAX = "2026-07-02"
FIRE_LOCAL_HOUR = 12  # midday local: morning warming observed, peak not yet locked


def _in_window(res_date: str) -> bool:
    return DEPTH_WINDOW_MIN <= str(res_date)[:10] <= DEPTH_WINDOW_MAX


def _has_depth(val) -> bool:
    if val is None:
        return False
    try:
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass
    return str(val).strip() not in ("", "null", "None", "[]")


def _row_has_depth(row: dict) -> bool:
    return _has_depth(row.get("orderbook_bids_json")) or _has_depth(row.get("orderbook_asks_json"))


# ── weather_obs: load once, same filter as settlement truth ─────────────────────────

def _load_obs() -> pd.DataFrame:
    """All settlement-grade METAR obs (source/obs_type match fetch_resolution_truth).

    Returns a frame with city, observed_at (UTC-naive as stored), temp_f, and the city-local
    date + local hour. Only cities in CITY_TZ (the gradeable universe) are kept.
    """
    import duckdb
    bt = __import__("51_snapshot_backtest")
    cities = "(" + ",".join("'" + c + "'" for c in CITY_TZ) + ")"
    with duckdb.connect(str(bt.RESEARCH_DB), read_only=True) as con:
        df = con.execute(f"""
            SELECT city, observed_at, temp_c * 9.0/5.0 + 32.0 AS temp_f
            FROM weather_obs
            WHERE source='historical_archive' AND obs_type='metar_hourly'
              AND city IN {cities}
            ORDER BY city, observed_at
        """).df()
    # observed_at is a naive wall-clock UTC timestamp; localize then convert per city.
    df["observed_at"] = pd.to_datetime(df["observed_at"]).dt.tz_localize("UTC")
    loc_date, loc_hour = [], []
    for city, ts in zip(df["city"], df["observed_at"]):
        tz = ZoneInfo(CITY_TZ[city])
        lt = ts.tz_convert(tz)
        loc_date.append(lt.strftime("%Y-%m-%d"))
        loc_hour.append(lt.hour)
    df["local_date"] = loc_date
    df["local_hour"] = loc_hour
    return df[df["temp_f"].notna()].reset_index(drop=True)


class RemainingWarmingModel:
    """Empirical distribution of (final_daily_max - running_max_so_far) by local hour.

    Trained on a set of (city, local_date) day-trajectories: for each such day and each local
    hour h present, remaining_warming(h) = day_final_max - max(temp up to and including h).
    dist_for_hour(h) returns [(delta_f, weight), ...] uniform over the pooled samples.
    """

    def __init__(self):
        self._by_hour: dict[int, list[float]] = defaultdict(list)

    @classmethod
    def train(cls, obs: pd.DataFrame, before_local_date: str) -> "RemainingWarmingModel":
        """Train ONLY on local-days strictly before `before_local_date` (leak-free)."""
        m = cls()
        train = obs[(obs["local_date"] < before_local_date) & obs["temp_f"].notna()]
        for (_city, _ld), g in train.groupby(["city", "local_date"]):
            g = g.sort_values("observed_at")
            temps = g["temp_f"].to_numpy()
            hours = g["local_hour"].to_numpy()
            if len(temps) == 0:
                continue
            final_max = float(temps.max())
            run = -1e9
            for t, h in zip(temps, hours):
                run = max(run, float(t))
                m._by_hour[int(h)].append(final_max - run)  # >= 0
        return m

    def dist_for_hour(self, hour: int):
        samples = self._by_hour.get(int(hour))
        if not samples:
            # fall back to the nearest hour that has data (within +-2h), else empty
            for dh in (1, -1, 2, -2):
                s = self._by_hour.get(int(hour) + dh)
                if s:
                    samples = s
                    break
        if not samples:
            return []
        w = 1.0 / len(samples)
        return [(d, w) for d in samples]


# ── fire-time selection at a local clock hour ───────────────────────────────────────

def _fire_row_at_local_hour(cd: CityDay, obs: pd.DataFrame, local_hour: int):
    """Pick the decision snapshot nearest to `local_hour` on the resolution local-day, then
    reconstruct the lookahead-safe running max at that fire time.

    Returns (fire_ts_utc, decision_rows, running_max_f, local_hour_actual) or None.
    """
    tz = ZoneInfo(CITY_TZ[cd.city])
    snaps = cd.snapshots
    if snaps is None or len(snaps) == 0:
        return None
    df = snaps.copy()
    df["_ts"] = pd.to_datetime(df["snapshot_at_utc"], errors="coerce", utc=True)
    df = df.dropna(subset=["_ts"])
    if df.empty:
        return None

    # Target instant = local_hour:00 on the resolution local-date, expressed in UTC.
    target_local = pd.Timestamp(f"{cd.resolution_date} {local_hour:02d}:00:00", tz=tz)
    target_utc = target_local.tz_convert("UTC")

    # Only consider snapshots at or before target (a real trader can't use a future snapshot);
    # pick the latest such snapshot. If none exist before target, abstain (market opened late).
    prior = df[df["_ts"] <= target_utc]
    if prior.empty:
        return None
    fire_ts = prior["_ts"].max()
    at = df[df["_ts"] == fire_ts]

    from bakeoff.harness.decision import _FIELDS
    rows = []
    for _, g in at.groupby("sub_market_condition_id"):
        r = g.iloc[0]
        rows.append({f: r[f] for f in _FIELDS}
                    | {"sub_market_condition_id": r["sub_market_condition_id"]})
    if not any(_row_has_depth(r) for r in rows):
        return None

    # Running max: settlement-grade obs on this city's local resolution-day, observed_at<=fire.
    day_obs = obs[(obs["city"] == cd.city) & (obs["local_date"] == cd.resolution_date)
                  & (obs["observed_at"] <= fire_ts)]
    if day_obs.empty:
        return None  # no observed temperature yet -> no nowcast signal
    running_max_f = float(day_obs["temp_f"].max())
    fire_local_hour = fire_ts.tz_convert(tz).hour
    return str(fire_ts), rows, running_max_f, fire_local_hour


def run(fee_rate: float, *, parquet_dir: str = PARQUET_DIR,
        local_hour: int = FIRE_LOCAL_HOUR, edge: float = c8.DEFAULT_EDGE) -> dict:
    """Score candidate #8 over the depth-covered, truth-gradeable window at a local fire hour."""
    obs = _load_obs()
    all_cds = load_city_days(parquet_dir)
    cds = [cd for cd in all_cds if _in_window(cd.resolution_date)
           and cd.truth_f is not None and cd.city in CITY_TZ]

    trades: list[dict] = []
    fired, abstained = 0, 0
    for cd in cds:
        picked = _fire_row_at_local_hour(cd, obs, local_hour)
        if picked is None:
            abstained += 1
            continue
        _ts, rows, running_max_f, fire_local_hour = picked
        model = RemainingWarmingModel.train(obs, before_local_date=cd.resolution_date)
        legs = c8.evaluate(cd, fee_rate, {"model": model},
                           decision_snapshots=rows, running_max_f=running_max_f,
                           local_hour=fire_local_hour, edge=edge)
        if legs:
            fired += 1
            trades += legs
        else:
            abstained += 1

    truth_by_key = {(cd.city, cd.resolution_date): cd.truth_f for cd in cds}
    res = score(trades, truth_by_key)
    res["n_city_days_considered"] = len(cds)
    res["n_fired"] = fired
    res["n_abstained"] = abstained
    res["fire_local_hour"] = local_hour
    res["edge_threshold"] = edge
    res["fee_regime"] = "opt" if fee_rate == 0.0 else "pess"
    return res


if __name__ == "__main__":
    import json
    print("=== candidate #8 intraday nowcast ===")
    for fee in (0.0, 0.05):
        r = run(fee)
        print(json.dumps({k: v for k, v in r.items() if k != "per_city_roi"},
                         indent=2, default=str))
