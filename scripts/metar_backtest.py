"""
METAR backtest replay — gate for LIVE_METAR_EXIT_ON_LOCK flag flip.

Replays the lock condition from executor/weather_exit.py:95-163 against:
  (a) closed live trades in weather_bot.db (small, n ≈ 2-34)
  (b) D1 panel markets in research_db/research.duckdb (large, statistical-power
      supplement for false-positive rate estimation)

Lock conditions evaluated:
  - Lock-YES: ">=" market AND direction='no' AND running daily-max ≥ threshold
  - Lock-NO : "<=" market AND direction='yes' AND running daily-max > threshold

The "conservative lock-NO" case (">=" market AND direction='yes' AND
ensemble P(YES) < 1%) is NOT replayed — requires a live-time ensemble snapshot
that isn't stored in metar_observations or weather_obs.

Outputs:
  scripts/_out_metar_backtest.csv      — live cohort, per closed trade
  scripts/_out_metar_backtest_d1.csv   — D1 cohort, per market

Gate per the 2026-04-22 plan:
  PASS: zero false positives in either cohort + at least one recovered-loss in live
  FAIL: any false positive in either cohort
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from layers.layer3_weather import parse_threshold_c
from markets.metar_observer import CITY_RESOLVERS

BOT_DB        = REPO_ROOT / "weather_bot.db"
RES_DB        = REPO_ROOT / "research_db" / "research.duckdb"
OUT_LIVE      = REPO_ROOT / "scripts" / "_out_metar_backtest.csv"
OUT_LIVE_HIST = REPO_ROOT / "scripts" / "_out_metar_backtest_live_hist.csv"
OUT_D1        = REPO_ROOT / "scripts" / "_out_metar_backtest_d1.csv"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_day(dt_utc: datetime, tz_name: str) -> date:
    return dt_utc.astimezone(ZoneInfo(tz_name)).date()


def _target_day_from_end_date(end_date_str: str | None) -> date | None:
    """end_date in trades is the local resolution date.
    Format may be 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SSZ' — the date portion
    is the local-tz resolution day in either case (Polymarket convention)."""
    if not end_date_str or len(end_date_str) < 10:
        return None
    try:
        return date.fromisoformat(end_date_str[:10])
    except ValueError:
        return None


def _local_day_utc_bounds(d: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start_local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    end_local   = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _city_tz(city: str) -> str | None:
    return (CITY_RESOLVERS.get((city or "").strip().lower()) or {}).get("tz")


# ── Lock evaluation (mirrors executor/weather_exit.py:95-163, cases 1+2) ─────

@dataclass
class LockResult:
    triggered:       bool
    case:            str | None        # "lock_yes" | "lock_no" | None
    trigger_at:      datetime | None
    trigger_temp_c:  float | None
    threshold_c:     float | None
    implied_outcome: str | None        # "YES" | "NO" | None — what the lock asserts will happen


def evaluate_lock(
    obs_iter,                          # iterable of (observed_at_utc, temp_c)
    op: str,                           # ">=" | "<="
    threshold_c: float,
    direction: str,                    # "yes" | "no"
) -> LockResult:
    direction = direction.lower()
    running_max: float | None = None
    for ts, t in obs_iter:
        if t is None:
            continue
        running_max = t if running_max is None else max(running_max, t)

        # Case 1: Lock-YES (">=" market, NO position) — observed max ≥ threshold
        if op == ">=" and direction == "no" and running_max >= threshold_c:
            return LockResult(True, "lock_yes", ts, running_max, threshold_c, "YES")

        # Case 2: Lock-NO ("<=" market, YES position) — observed max > threshold
        if op == "<=" and direction == "yes" and running_max > threshold_c:
            return LockResult(True, "lock_no", ts, running_max, threshold_c, "NO")

    return LockResult(False, None, None, None, threshold_c, None)


# ── Live cohort: weather_bot.db closed trades ────────────────────────────────

LIVE_COLS = [
    "trade_id", "city", "direction", "threshold_str", "threshold_op", "threshold_c",
    "opened_at", "closed_at", "end_date", "target_local_day",
    "actual_resolution", "actual_pnl", "actual_pnl_pct",
    "triggered", "case", "trigger_at", "trigger_temp_c",
    "hours_after_open", "hours_before_actual_close", "lock_correct",
    "classification", "n_obs_in_window",
]


def _classify_live(triggered: bool, lock_correct: bool | None, actual_pnl: float | None) -> str:
    if not triggered:
        return "no_trigger"
    if lock_correct is False:
        return "false_positive"
    if actual_pnl is not None and actual_pnl < 0:
        return "recovered_loss"
    return "neutral_confirmation"


def run_live_cohort() -> list[dict]:
    rows: list[dict] = []
    if not BOT_DB.exists():
        print(f"[live] {BOT_DB} not found — skipping live cohort")
        return rows

    conn = sqlite3.connect(str(BOT_DB))
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, city, direction, threshold, opened_at, closed_at, end_date,
               actual_resolution, pnl, pnl_pct
          FROM trades
         WHERE status = 'closed' AND closed_at IS NOT NULL AND city IS NOT NULL
         ORDER BY id
    """).fetchall()

    for tr in trades:
        city  = tr["city"]
        direction = (tr["direction"] or "").lower()
        thresh_raw = tr["threshold"] or ""
        parsed = parse_threshold_c(thresh_raw)
        opened_at = _parse_iso(tr["opened_at"])
        closed_at = _parse_iso(tr["closed_at"])
        end_date  = _parse_iso(tr["end_date"])

        row = {
            "trade_id":          tr["id"],
            "city":              city,
            "direction":         direction,
            "threshold_str":     thresh_raw,
            "threshold_op":      parsed[0] if parsed else None,
            "threshold_c":       round(parsed[1], 3) if parsed else None,
            "opened_at":         tr["opened_at"],
            "closed_at":         tr["closed_at"],
            "end_date":          tr["end_date"],
            "target_local_day":  None,
            "actual_resolution": tr["actual_resolution"],
            "actual_pnl":        tr["pnl"],
            "actual_pnl_pct":    tr["pnl_pct"],
            "triggered":         False,
            "case":              None,
            "trigger_at":        None,
            "trigger_temp_c":    None,
            "hours_after_open":  None,
            "hours_before_actual_close": None,
            "lock_correct":      None,
            "classification":    "no_trigger",
            "n_obs_in_window":   0,
        }

        # Skip rows the lock would never evaluate (case 3 deferred).
        if parsed is None or end_date is None or opened_at is None:
            row["classification"] = "skipped_unparseable"
            rows.append(row)
            continue
        op, threshold_c = parsed
        applies = (op == ">=" and direction == "no") or (op == "<=" and direction == "yes")
        if not applies:
            row["classification"] = "skipped_case3_or_inapplicable"
            rows.append(row)
            continue

        tz_name = _city_tz(city)
        if tz_name is None:
            row["classification"] = "skipped_no_tz"
            rows.append(row)
            continue

        target_day = _target_day_from_end_date(tr["end_date"])
        if target_day is None:
            row["classification"] = "skipped_bad_end_date"
            rows.append(row)
            continue
        row["target_local_day"] = target_day.isoformat()
        day_start_utc, day_end_utc = _local_day_utc_bounds(target_day, tz_name)

        # Window: [opened_at, closed_at] AND constrained to local target day.
        win_start = max(opened_at, day_start_utc)
        win_end   = min(closed_at or day_end_utc, day_end_utc)
        if win_start >= win_end:
            row["classification"] = "skipped_empty_window"
            rows.append(row)
            continue

        # Pull all city obs and filter in Python to avoid lex-compare issues with
        # mixed timezone offsets (e.g. HKO rows stored as +08:00).
        raw_obs = conn.execute("""
            SELECT observed_at_utc, observed_temp_c
              FROM metar_observations
             WHERE city = ?
             ORDER BY observed_at_utc ASC
        """, (city,)).fetchall()

        obs = []
        for o in raw_obs:
            ts = _parse_iso(o["observed_at_utc"])
            if ts is None:
                continue
            if ts < win_start or ts >= win_end:
                continue
            obs.append((ts, o["observed_temp_c"]))

        row["n_obs_in_window"] = len(obs)

        def _iter():
            for ts, t in obs:
                yield ts, t

        result = evaluate_lock(_iter(), op, threshold_c, direction)
        row["triggered"]      = result.triggered
        row["case"]           = result.case
        row["trigger_at"]     = result.trigger_at.isoformat() if result.trigger_at else None
        row["trigger_temp_c"] = round(result.trigger_temp_c, 2) if result.trigger_temp_c is not None else None

        if result.triggered and result.trigger_at:
            row["hours_after_open"] = round((result.trigger_at - opened_at).total_seconds() / 3600, 2)
            if closed_at:
                row["hours_before_actual_close"] = round((closed_at - result.trigger_at).total_seconds() / 3600, 2)
            actual = (tr["actual_resolution"] or "").upper() or None
            if actual is not None:
                row["lock_correct"] = (result.implied_outcome == actual)
        row["classification"] = _classify_live(result.triggered, row["lock_correct"], tr["pnl"])
        rows.append(row)

    conn.close()
    return rows


# ── D1 cohort: research_db/research.duckdb threshold markets ─────────────────

D1_COLS = [
    "condition_id", "city", "threshold_value_c", "threshold_direction", "target_date",
    "resolution_outcome", "daily_max_c",
    "triggered", "case", "trigger_at", "trigger_temp_c",
    "lock_correct", "classification", "n_obs_in_window",
]


def _classify_d1(triggered: bool, lock_correct: bool | None) -> str:
    if not triggered:
        return "no_trigger"
    if lock_correct is False:
        return "false_positive"
    return "true_positive"


def run_live_cohort_against_research_db() -> list[dict]:
    """Replay closed live trades against research_db.weather_obs (metar_hourly +
    hko_10min only). This unlocks all 34 live trades for replay — the bot DB's
    metar_observations only goes back to 2026-04-29, but research_db covers the
    full V1 window."""
    rows: list[dict] = []
    if not BOT_DB.exists() or not RES_DB.exists():
        print(f"[live-hist] DB(s) missing — skipping")
        return rows

    conn = sqlite3.connect(str(BOT_DB))
    conn.row_factory = sqlite3.Row
    trades = conn.execute("""
        SELECT id, city, direction, threshold, opened_at, closed_at, end_date,
               actual_resolution, pnl, pnl_pct
          FROM trades
         WHERE status = 'closed' AND closed_at IS NOT NULL AND city IS NOT NULL
         ORDER BY id
    """).fetchall()
    conn.close()

    rdb = duckdb.connect(str(RES_DB), read_only=True)

    for tr in trades:
        city  = tr["city"]
        direction = (tr["direction"] or "").lower()
        thresh_raw = tr["threshold"] or ""
        parsed = parse_threshold_c(thresh_raw)
        opened_at = _parse_iso(tr["opened_at"])
        closed_at = _parse_iso(tr["closed_at"])

        row = {
            "trade_id":          tr["id"],
            "city":              city,
            "direction":         direction,
            "threshold_str":     thresh_raw,
            "threshold_op":      parsed[0] if parsed else None,
            "threshold_c":       round(parsed[1], 3) if parsed else None,
            "opened_at":         tr["opened_at"],
            "closed_at":         tr["closed_at"],
            "end_date":          tr["end_date"],
            "target_local_day":  None,
            "actual_resolution": tr["actual_resolution"],
            "actual_pnl":        tr["pnl"],
            "actual_pnl_pct":    tr["pnl_pct"],
            "triggered":         False,
            "case":              None,
            "trigger_at":        None,
            "trigger_temp_c":    None,
            "hours_after_open":  None,
            "hours_before_actual_close": None,
            "lock_correct":      None,
            "classification":    "no_trigger",
            "n_obs_in_window":   0,
        }
        if parsed is None or opened_at is None:
            row["classification"] = "skipped_unparseable"
            rows.append(row)
            continue
        op, threshold_c = parsed
        applies = (op == ">=" and direction == "no") or (op == "<=" and direction == "yes")
        if not applies:
            row["classification"] = "skipped_case3_or_inapplicable"
            rows.append(row)
            continue

        tz_name = _city_tz(city)
        if tz_name is None:
            row["classification"] = "skipped_no_tz"
            rows.append(row)
            continue

        target_day = _target_day_from_end_date(tr["end_date"])
        if target_day is None:
            row["classification"] = "skipped_bad_end_date"
            rows.append(row)
            continue
        row["target_local_day"] = target_day.isoformat()
        day_start_utc, day_end_utc = _local_day_utc_bounds(target_day, tz_name)

        win_start = max(opened_at, day_start_utc)
        win_end   = min(closed_at or day_end_utc, day_end_utc)
        if win_start >= win_end:
            row["classification"] = "skipped_empty_window"
            rows.append(row)
            continue

        obs = rdb.execute("""
            SELECT observed_at, temp_c
              FROM weather_obs
             WHERE city = ?
               AND observed_at >= ?
               AND observed_at <  ?
               AND obs_type IN ('metar_hourly', 'hko_10min')
               AND temp_c IS NOT NULL
             ORDER BY observed_at ASC
        """, [city, win_start.replace(tzinfo=None), win_end.replace(tzinfo=None)]).fetchall()

        row["n_obs_in_window"] = len(obs)

        def _iter():
            for ts, t in obs:
                if isinstance(ts, datetime) and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                yield ts, t

        result = evaluate_lock(_iter(), op, threshold_c, direction)
        row["triggered"]      = result.triggered
        row["case"]           = result.case
        row["trigger_at"]     = result.trigger_at.isoformat() if result.trigger_at else None
        row["trigger_temp_c"] = round(result.trigger_temp_c, 2) if result.trigger_temp_c is not None else None

        if result.triggered and result.trigger_at:
            row["hours_after_open"] = round((result.trigger_at - opened_at).total_seconds() / 3600, 2)
            if closed_at:
                row["hours_before_actual_close"] = round((closed_at - result.trigger_at).total_seconds() / 3600, 2)
            actual = (tr["actual_resolution"] or "").upper() or None
            if actual is not None:
                row["lock_correct"] = (result.implied_outcome == actual)
        row["classification"] = _classify_live(result.triggered, row["lock_correct"], tr["pnl"])
        rows.append(row)

    rdb.close()
    return rows


def run_d1_cohort(window_start: date, window_end: date) -> list[dict]:
    rows: list[dict] = []
    if not RES_DB.exists():
        print(f"[d1] {RES_DB} not found — skipping D1 augmentation")
        return rows

    rdb = duckdb.connect(str(RES_DB), read_only=True)

    # Pull threshold markets resolved within window.
    # Note: research_db stores threshold_direction as 'gte'/'lte', not '>='/'<='.
    # Restrict to "highest temperature" markets — the bot's METAR lock targets
    # daily MAX, so "lowest temperature" markets are out of scope.
    markets = rdb.execute("""
        SELECT condition_id, city, threshold_value,
               CASE threshold_direction WHEN 'gte' THEN '>=' WHEN 'lte' THEN '<=' END AS op,
               target_date, resolution_outcome
          FROM markets
         WHERE market_type = 'threshold'
           AND threshold_direction IN ('gte', 'lte')
           AND target_date BETWEEN ? AND ?
           AND resolution_outcome IN ('YES', 'NO')
           AND LOWER(question) LIKE '%highest%'
         ORDER BY target_date, city
    """, [window_start, window_end]).fetchall()

    # Pull daily_max ground truth for the same window.
    res_map: dict[tuple[str, date], float | None] = {}
    for r in rdb.execute("""
        SELECT city, date_local, daily_max_c
          FROM resolutions
         WHERE date_local BETWEEN ? AND ?
    """, [window_start, window_end]).fetchall():
        res_map[(r[0], r[1])] = r[2]

    for m in markets:
        cond_id, city, thresh_value_c, op, target_d, actual = m
        # Direction implied by lock semantics: ">=" → hypothetical NO position; "<=" → hypothetical YES.
        if op == ">=":
            hypothetical_dir = "no"
        elif op == "<=":
            hypothetical_dir = "yes"
        else:
            continue

        tz_name = _city_tz(city)
        row = {
            "condition_id":         cond_id,
            "city":                 city,
            "threshold_value_c":    round(float(thresh_value_c), 3) if thresh_value_c is not None else None,
            "threshold_direction":  op,
            "target_date":          target_d.isoformat(),
            "resolution_outcome":   actual,
            "daily_max_c":          res_map.get((city, target_d)),
            "triggered":            False,
            "case":                 None,
            "trigger_at":           None,
            "trigger_temp_c":       None,
            "lock_correct":         None,
            "classification":       "no_trigger",
            "n_obs_in_window":      0,
        }
        if tz_name is None or thresh_value_c is None:
            row["classification"] = "skipped_no_tz_or_threshold"
            rows.append(row)
            continue

        day_start_utc, day_end_utc = _local_day_utc_bounds(target_d, tz_name)

        # Restrict to obs sources Polymarket actually resolves on. asos_5min has
        # sub-hourly spikes that don't appear in the official hourly METAR.
        # Strip tz from params — duckdb's TIMESTAMP column is naive UTC and
        # tz-aware params get reinterpreted via system local time.
        obs = rdb.execute("""
            SELECT observed_at, temp_c
              FROM weather_obs
             WHERE city = ?
               AND observed_at >= ?
               AND observed_at <  ?
               AND obs_type IN ('metar_hourly', 'hko_10min')
               AND temp_c IS NOT NULL
             ORDER BY observed_at ASC
        """, [city, day_start_utc.replace(tzinfo=None), day_end_utc.replace(tzinfo=None)]).fetchall()

        row["n_obs_in_window"] = len(obs)

        def _iter():
            for ts, t in obs:
                if isinstance(ts, datetime) and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                yield ts, t

        result = evaluate_lock(_iter(), op, float(thresh_value_c), hypothetical_dir)
        row["triggered"]      = result.triggered
        row["case"]           = result.case
        row["trigger_at"]     = result.trigger_at.isoformat() if result.trigger_at else None
        row["trigger_temp_c"] = round(result.trigger_temp_c, 2) if result.trigger_temp_c is not None else None
        if result.triggered:
            row["lock_correct"] = (result.implied_outcome == actual)
        row["classification"] = _classify_d1(result.triggered, row["lock_correct"])
        rows.append(row)

    rdb.close()
    return rows


# ── Output + summary ─────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _summary(label: str, rows: list[dict], live: bool) -> dict:
    counts: dict = {}
    for r in rows:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    n_total = len(rows)
    n_evaluated = sum(1 for r in rows if not str(r["classification"]).startswith("skipped"))
    n_trigger = sum(1 for r in rows if r["triggered"])
    n_fp = counts.get("false_positive", 0)
    print(f"\n=== {label} ({n_total} rows, {n_evaluated} evaluated) ===")
    for k in sorted(counts):
        print(f"  {k:32s} {counts[k]}")
    print(f"  triggered total                  {n_trigger}")
    if n_evaluated:
        fp_rate = n_fp / n_evaluated
        print(f"  FP rate (FP / evaluated)         {fp_rate:.2%}")
    return {"n_total": n_total, "n_evaluated": n_evaluated, "n_trigger": n_trigger, "n_fp": n_fp, "counts": counts}


def main() -> int:
    print("=== METAR backtest replay ===")
    print(f"bot DB: {BOT_DB}")
    print(f"d1 DB:  {RES_DB}")

    live_rows = run_live_cohort()
    _write_csv(OUT_LIVE, live_rows, LIVE_COLS)
    print(f"wrote {OUT_LIVE} ({len(live_rows)} rows)")

    live_hist_rows = run_live_cohort_against_research_db()
    _write_csv(OUT_LIVE_HIST, live_hist_rows, LIVE_COLS)
    print(f"wrote {OUT_LIVE_HIST} ({len(live_hist_rows)} rows)")

    # D1 window: V1 panel range. Memory: 2026-01-28 → 2026-04-28.
    d1_rows = run_d1_cohort(date(2026, 1, 28), date(2026, 4, 28))
    _write_csv(OUT_D1, d1_rows, D1_COLS)
    print(f"wrote {OUT_D1} ({len(d1_rows)} rows)")

    live_sum      = _summary("LIVE COHORT (shadow obs)",       live_rows,      live=True)
    live_hist_sum = _summary("LIVE COHORT (research_db obs)",  live_hist_rows, live=True)
    d1_sum        = _summary("D1 COHORT",                       d1_rows,        live=False)

    # Gate decision — combined live (real trades) + D1 (synthetic, statistical power)
    combined_live_fp = live_sum["n_fp"] + live_hist_sum["n_fp"]
    combined_recovered = (live_sum["counts"].get("recovered_loss", 0)
                          + live_hist_sum["counts"].get("recovered_loss", 0))
    print("\n=== GATE DECISION ===")
    print(f"Live FP total (shadow + research_db replay): {combined_live_fp}")
    print(f"Live recovered-loss total:                   {combined_recovered}")
    print(f"D1 FP rate (synthetic):                      "
          f"{d1_sum['n_fp']}/{d1_sum['n_evaluated']} "
          f"= {d1_sum['n_fp']/max(d1_sum['n_evaluated'],1):.2%}")
    if combined_live_fp > 0:
        print(f"FAIL — {combined_live_fp} false positive(s) on real bot trades.")
        print("       Any live-replay FP is catastrophic per the 2026-04-22 plan.")
        return 1
    if d1_sum["n_fp"] / max(d1_sum["n_evaluated"], 1) > 0.01:
        print(f"FAIL — D1 FP rate {d1_sum['n_fp']/max(d1_sum['n_evaluated'],1):.2%} > 1% gate.")
        print("       Synthetic replay implies real FP risk under broader trade volume.")
        return 1
    if combined_recovered == 0:
        print("WARN — zero FP, but no recovered-loss case demonstrated in live replay.")
        print("       Trigger is safe but not yet shown to have positive EV.")
        return 2
    print(f"PASS — zero live FPs; D1 FP rate <= 1%; "
          f"{combined_recovered} recovered-loss case(s) demonstrated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
