"""
Weather Calibration Utility
-----------------------------
Two independent backfill passes + a calibration report.

Usage:
  python weather_calibration.py backfill   # run both passes, safe to re-run anytime
  python weather_calibration.py report     # print calibration accuracy tables

── Pass 1: Resolution (CLOB midpoint) ────────────────────────────────────────
  Targets: early-exit closed trades with no actual_resolution yet.
  Method:  CLOB /midpoint on stored YES token_id.
  Writes:  actual_resolution, forecast_correct, resolution_price,
           resolution_fetched_at
  Skips:   markets still settling (0.02 < price < 0.98), 404s (token gone).

── Pass 2: Temperature (Open-Meteo archive) ──────────────────────────────────
  Targets: any closed trade (any exit type) with no actual_temperature yet
           whose end_date is in the past and whose city is in CITY_COORDS.
  Method:  ONE archive API call per city covering all needed dates in a single
           request (date-range batching). 0.5s delay between city calls.
  Writes:  actual_temperature, temperature_delta
  Skips:   cities not in CITY_COORDS, already-populated rows, future dates.

── DB-as-cache ────────────────────────────────────────────────────────────────
  Both passes skip rows where the target columns are already populated.
  Re-running is always safe and cheap — previously resolved rows are ignored.

── temperature_delta sign convention ─────────────────────────────────────────
  Positive = condition satisfied = resolves YES
  Negative = condition missed    = resolves NO

  For >= markets:  delta = actual_temp_C - threshold_C
  For <= markets:  delta = threshold_C   - actual_temp_C

  |delta| magnitude tells you how "easy" the call was:
    |delta| >= 5°C  → clear outcome
    2° <= |delta| < 5°  → moderate margin
    |delta| < 2°C  → coin-flip territory; no model should be confident here
"""
import re
import sys
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

import db
from config import POLYMARKET_CLOB_API, OPEN_METEO_ARCHIVE_API
from layers.layer3_weather import CITY_COORDS

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-bot/1.0"})

_CLOB_YES  = 0.98
_CLOB_NO   = 0.02
_THRESHOLD_RE = re.compile(r'^([<>]=?)(\d+(?:\.\d+)?)([CF])$')


# ── Threshold helpers ─────────────────────────────────────────────────────────

def _parse_threshold(threshold_str: str) -> tuple[str, float] | None:
    """
    Parse '>=74F', '<=33C' etc → (operator, value_in_celsius).
    Returns None if the string can't be parsed.
    """
    m = _THRESHOLD_RE.match((threshold_str or "").strip())
    if not m:
        return None
    op    = m.group(1)                  # '>=' or '<='
    value = float(m.group(2))
    unit  = m.group(3).upper()
    if unit == "F":
        value = (value - 32.0) * 5.0 / 9.0
    return op, value


def _temperature_delta(actual_c: float, op: str, threshold_c: float) -> float:
    """
    Signed margin where positive = condition met = resolves YES.
    >= markets: actual - threshold  (positive when actual >= threshold)
    <= markets: threshold - actual  (positive when actual <= threshold)
    """
    return actual_c - threshold_c if op == ">=" else threshold_c - actual_c


# ── Pass 1: CLOB resolution ───────────────────────────────────────────────────

def _clob_midpoint(token_id: str) -> float | None:
    try:
        resp = _session.get(
            f"{POLYMARKET_CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        mid = resp.json().get("mid")
        return float(mid) if mid is not None else None
    except Exception:
        return None


def _run_resolution_pass(quiet: bool = False) -> dict:
    all_trades = db.get_all_trades()
    candidates = [
        t for t in all_trades
        if t.get("status") == "closed"
        and t.get("exit_reason")
        and "resolved" not in (t.get("exit_reason") or "").lower()
        and t.get("actual_resolution") is None
        and t.get("token_id")
    ]

    counts = {"resolved": 0, "settling": 0, "no_data": 0}

    if not candidates:
        if not quiet:
            print("  [resolution] Nothing to backfill.")
        return counts

    if not quiet:
        print(f"  [resolution] Checking {len(candidates)} early-exit trade(s)...")

    now_iso = datetime.now(timezone.utc).isoformat()

    for trade in candidates:
        yes_price = _clob_midpoint(trade["token_id"])

        if yes_price is None:
            counts["no_data"] += 1
            label = "404 / no data"
        elif yes_price >= _CLOB_YES:
            actual = "YES"
        elif yes_price <= _CLOB_NO:
            actual = "NO"
        else:
            counts["settling"] += 1
            if not quiet:
                city = (trade.get("city") or "?")[:12]
                print(f"    skip  {city:<12}  still settling (YES={yes_price:.3f})")
            time.sleep(0.15)
            continue

        if yes_price is None:
            if not quiet:
                city = (trade.get("city") or "?")[:12]
                print(f"    skip  {city:<12}  {label}")
            time.sleep(0.15)
            continue

        direction = (trade.get("direction") or "").upper()
        correct   = 1 if direction == actual else 0

        db.update_trade(trade["id"], {
            "actual_resolution":    actual,
            "forecast_correct":     correct,
            "resolution_price":     round(yes_price, 4),
            "resolution_fetched_at": now_iso,
        })
        counts["resolved"] += 1

        if not quiet:
            city  = (trade.get("city") or "?")[:12]
            label = "✓ correct" if correct else "✗ wrong"
            fill  = trade.get("fill_price") or 0
            print(f"    saved {city:<12}  dir={direction} actual={actual} {label}  "
                  f"fill={fill:.3f}  resolved_price={yes_price:.4f}")

        time.sleep(0.15)

    if not quiet:
        print(f"  [resolution] resolved={counts['resolved']}  "
              f"settling={counts['settling']}  no_data={counts['no_data']}")
    return counts


# ── Pass 2: Archive temperature ───────────────────────────────────────────────

def _fetch_archive_for_city(city: str, dates: set[str]) -> dict[str, float]:
    """
    ONE API call: fetch temperature_2m_max for all needed dates for a single city.
    Returns {date_str: temp_celsius}. Empty dict on error.
    """
    coords = CITY_COORDS.get(city)
    if not coords:
        return {}

    start = min(dates)
    end   = max(dates)

    try:
        resp = _session.get(
            OPEN_METEO_ARCHIVE_API,
            params={
                "latitude":   coords["lat"],
                "longitude":  coords["lon"],
                "start_date": start,
                "end_date":   end,
                "daily":      "temperature_2m_max",
                "timezone":   coords.get("tz", "UTC"),
            },
            timeout=15,
        )
        resp.raise_for_status()
        data  = resp.json()
        times = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        return {
            t: float(v)
            for t, v in zip(times, temps)
            if v is not None
        }
    except Exception as e:
        print(f"    [temp] Archive error for {city} ({start}→{end}): {e}")
        return {}


def _run_temperature_pass(quiet: bool = False) -> dict:
    all_trades = db.get_all_trades()
    now        = datetime.now(timezone.utc)

    candidates = [
        t for t in all_trades
        if t.get("status") == "closed"
        and t.get("actual_temperature") is None
        and t.get("city")
        and t.get("end_date")
        and t.get("threshold")
    ]

    # Filter to trades whose end_date has passed (no point querying archive for future)
    def is_past(t):
        try:
            ed = datetime.fromisoformat(t["end_date"].replace("Z", "+00:00"))
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            return ed < now
        except Exception:
            return False

    candidates = [t for t in candidates if is_past(t)]

    counts = {"filled": 0, "no_coords": 0, "no_archive": 0, "bad_threshold": 0}

    if not candidates:
        if not quiet:
            print("  [temperature] Nothing to backfill.")
        return counts

    # Group by city → collect all dates needed (one API call covers the whole range)
    city_dates: dict[str, set[str]] = defaultdict(set)
    for trade in candidates:
        city = trade["city"].lower()
        date = trade["end_date"][:10]   # YYYY-MM-DD
        if city in CITY_COORDS:
            city_dates[city].add(date)
        else:
            counts["no_coords"] += 1

    if not quiet:
        unique_cities = len(city_dates)
        total_dates   = sum(len(v) for v in city_dates.values())
        print(f"  [temperature] {len(candidates)} trade(s) across "
              f"{unique_cities} city/ies, {total_dates} unique date(s) → "
              f"{unique_cities} API call(s)")

    # One archive call per city
    archive: dict[tuple[str, str], float] = {}   # (city, date) → temp_c
    for i, (city, dates) in enumerate(city_dates.items()):
        if not quiet:
            print(f"    fetching {city} ({len(dates)} date(s): {sorted(dates)})")
        result = _fetch_archive_for_city(city, dates)
        for date, temp in result.items():
            archive[(city, date)] = temp
        if i < len(city_dates) - 1:
            time.sleep(0.5)   # gentle rate limiting between cities

    if not quiet:
        print(f"  [temperature] Archive returned {len(archive)} date/city values")

    # Write back to each trade
    for trade in candidates:
        city  = trade["city"].lower()
        date  = trade["end_date"][:10]
        temp  = archive.get((city, date))

        if temp is None:
            counts["no_archive"] += 1
            if not quiet:
                print(f"    skip  {city:<14}  {date}  no archive data")
            continue

        parsed = _parse_threshold(trade["threshold"])
        if parsed is None:
            counts["bad_threshold"] += 1
            if not quiet:
                print(f"    skip  {city:<14}  bad threshold: {trade['threshold']!r}")
            continue

        op, threshold_c = parsed
        delta = _temperature_delta(temp, op, threshold_c)

        db.update_trade(trade["id"], {
            "actual_temperature": round(temp, 2),
            "temperature_delta":  round(delta, 2),
        })
        counts["filled"] += 1

        if not quiet:
            condition_met = delta >= 0
            city_label    = city[:12]
            print(f"    saved {city_label:<12}  {date}  "
                  f"actual={temp:.1f}°C  threshold={trade['threshold']}  "
                  f"delta={delta:+.1f}°C  ({'YES' if condition_met else 'NO'})")

    if not quiet:
        print(f"  [temperature] filled={counts['filled']}  "
              f"no_coords={counts['no_coords']}  no_archive={counts['no_archive']}  "
              f"bad_threshold={counts['bad_threshold']}")
    return counts


# ── Combined backfill entry point ─────────────────────────────────────────────

def run_backfill():
    print("\n[calibration backfill]")
    print("─" * 50)
    print("Pass 1: Resolution (CLOB midpoint)")
    _run_resolution_pass()
    print()
    print("Pass 2: Temperature (Open-Meteo archive)")
    _run_temperature_pass()
    print()
    print("[calibration backfill] Done.")


# ── Calibration Report ────────────────────────────────────────────────────────

def run_report():
    all_trades = db.get_all_trades()
    closed     = [t for t in all_trades if t.get("status") == "closed"]

    # Augment held-to-resolution trades with synthetic actual_resolution from P&L
    # (they were settled by the resolver, not by CLOB backfill)
    augmented = []
    for t in closed:
        t = dict(t)
        if t.get("actual_resolution") is None:
            if "resolved" in (t.get("exit_reason") or "").lower():
                direction = (t.get("direction") or "").upper()
                pnl       = t.get("pnl") or 0
                if abs(pnl) > 0.01:
                    t["actual_resolution"] = direction if pnl > 0 else (
                        "NO" if direction == "YES" else "YES"
                    )
                    t["forecast_correct"] = 1 if pnl > 0 else 0
        augmented.append(t)

    known = [t for t in augmented if t.get("actual_resolution") is not None]

    if not known:
        print("\nNo calibration data yet. Run:  python weather_calibration.py backfill\n")
        return

    total   = len(known)
    correct = sum(1 for t in known if t.get("forecast_correct") == 1)
    has_temp = [t for t in known if t.get("actual_temperature") is not None]

    print(f"\n{'='*65}")
    print(f"  CALIBRATION REPORT  ({total} trades with known outcomes)")
    print(f"{'='*65}")
    print(f"  Overall forecast accuracy : {correct}/{total} = {correct/total*100:.1f}%")
    print(f"  With temperature data     : {len(has_temp)}/{total} trades")
    print()

    _breakdown(known, "Direction",  "direction")
    _breakdown(known, "Exit Type",  key_fn=_exit_label)
    _breakdown(known, "Tier",       key_fn=_tier_label)
    _breakdown(known, "City",       "city", top_n=10)
    _breakdown(known, "Ensemble Conviction", key_fn=_conviction_band)

    # Temperature delta section — only when we have data
    if has_temp:
        print(f"  By Temperature Margin (|delta| = degrees from threshold):")
        print(f"  {'Margin band':<32} {'Trades':>7} {'Correct':>8} {'Accuracy':>9} {'Net P&L':>9}  Notes")
        print("  " + "-" * 75)
        _margin_rows(has_temp)
        print()

        # Scatter: wrong calls by margin
        wrong_temp = [t for t in has_temp if t.get("forecast_correct") == 0]
        if wrong_temp:
            print(f"  Wrong calls with temperature data ({len(wrong_temp)}):")
            print(f"  {'City':<14} {'Dir':<4} {'Actual':<7} {'Delta':>7} {'Fill':>7} {'Exit'}")
            print("  " + "-" * 70)
            for t in sorted(wrong_temp, key=lambda x: abs(x.get("temperature_delta") or 0)):
                delta = t.get("temperature_delta")
                delta_str = f"{delta:+.1f}°C" if delta is not None else "?"
                print(f"  {(t.get('city') or '?'):<14} "
                      f"{(t.get('direction') or '?'):<4} "
                      f"{(t.get('actual_resolution') or '?'):<7} "
                      f"{delta_str:>7} "
                      f"{(t.get('fill_price') or 0):>7.3f}  "
                      f"{(t.get('exit_reason') or '')[:30]}")
        print()

    # Missed P&L from early exits
    early_resolved = [
        t for t in augmented
        if t.get("actual_resolution") is not None
        and t.get("exit_reason")
        and "resolved" not in (t.get("exit_reason") or "").lower()
        and t.get("resolution_price") is not None
    ]
    if early_resolved:
        total_missed = 0.0
        print(f"  Missed P&L from early exits ({len(early_resolved)} trades):")
        print(f"  {'City':<14} {'Dir':<4} {'Actual':<7} {'Fill':>6} {'Exit$':>6} "
              f"{'Settled':>8} {'Missed$':>8} {'Exit Reason'}")
        print("  " + "-" * 80)
        for t in early_resolved:
            res_price = t["resolution_price"]
            exit_price = t.get("exit_price") or 0
            shares     = t.get("shares") or 0
            direction  = (t.get("direction") or "").upper()

            # For YES trades: missed = (resolution_price - exit_price) * shares
            # For NO trades:  missed = ((1-resolution_price) - (1-exit_price)) * shares
            #                        = (exit_price - resolution_price) * shares
            if direction == "YES":
                missed = (res_price - exit_price) * shares
            else:
                missed = (exit_price - res_price) * shares

            total_missed += missed
            delta = t.get("temperature_delta")
            delta_str = f"({delta:+.1f}°)" if delta is not None else ""
            print(f"  {(t.get('city') or '?'):<14} "
                  f"{direction:<4} "
                  f"{(t.get('actual_resolution') or '?'):<7} "
                  f"{(t.get('fill_price') or 0):>6.3f} "
                  f"{exit_price:>6.3f} "
                  f"{res_price:>8.3f} "
                  f"{missed:>+8.2f}  "
                  f"{(t.get('exit_reason') or '')[:25]} {delta_str}")

        print(f"  {'':14} {'':4} {'':7} {'':6} {'':6} {'Total missed:':>8} {total_missed:>+8.2f}")
        print()


def _margin_rows(trades: list[dict]):
    bands = [
        ("blowout  (|Δ| ≥ 5°C)",   lambda d: abs(d) >= 5,        "should be easy to call"),
        ("moderate (2° ≤ |Δ| < 5°)", lambda d: 2 <= abs(d) < 5,  "model edge expected"),
        ("close    (|Δ| < 2°C)",    lambda d: abs(d) < 2,         "coin-flip zone"),
    ]
    for label, test, note in bands:
        group = [t for t in trades if t.get("temperature_delta") is not None
                 and test(t["temperature_delta"])]
        if not group:
            continue
        n       = len(group)
        correct = sum(1 for t in group if t.get("forecast_correct") == 1)
        pnl     = sum((t.get("pnl") or 0) for t in group)
        acc     = f"{correct/n*100:.0f}%"
        print(f"  {label:<32} {n:>7} {correct:>8} {acc:>9} {pnl:>+9.2f}  {note}")


def _breakdown(trades: list, title: str, key: str | None = None,
               key_fn=None, top_n: int | None = None):
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        k = key_fn(t) if key_fn else str(t.get(key) or "unknown")
        groups[k].append(t)

    rows = sorted(
        [(g, len(items), sum(1 for t in items if t.get("forecast_correct") == 1),
          sum((t.get("pnl") or 0) for t in items))
         for g, items in groups.items()],
        key=lambda r: r[1], reverse=True
    )
    if top_n:
        rows = rows[:top_n]

    print(f"  By {title}:")
    print(f"  {'Group':<32} {'Trades':>7} {'Correct':>8} {'Accuracy':>9} {'Net P&L':>9}")
    print("  " + "-" * 70)
    for group, n, correct, pnl in rows:
        acc = f"{correct/n*100:.0f}%" if n else "—"
        print(f"  {group:<32} {n:>7} {correct:>8} {acc:>9} {pnl:>+9.2f}")
    print()


def _exit_label(t: dict) -> str:
    r = (t.get("exit_reason") or "").lower()
    if "resolved" in r:  return "held to resolution"
    if "adverse"  in r:  return "adverse exit"
    if "spread"   in r:  return "spread exit"
    if "ensemble" in r:  return "ensemble flip"
    return "other"


def _tier_label(t: dict) -> str:
    edge = t.get("edge_score") or 0
    # edge_score is stored as percentage (e.g. 10.5 = 10.5%), not fraction
    if edge >= 30:  return "STRONG (>=30%)"
    if edge >= 15:  return "EDGE (15-30%)"
    if edge >  0:   return "WEAK (<15%)"
    return "unknown"


def _conviction_band(t: dict) -> str:
    pct = t.get("entry_ensemble_pct")
    if pct is None:
        return "unknown"
    pct = float(pct)
    if pct >= 0.85 or pct <= 0.15: return "near-unanimous (>=85% / <=15%)"
    if pct >= 0.70 or pct <= 0.30: return "strong (70-84% / 16-30%)"
    return "uncertain (31-69%) — should not trade"


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"

    if mode == "backfill":
        run_backfill()
    elif mode == "report":
        run_report()
    else:
        print("Usage:")
        print("  python weather_calibration.py backfill")
        print("  python weather_calibration.py report")
