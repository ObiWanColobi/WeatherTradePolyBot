"""Phase2-00 — Open-Meteo historical-forecast-api freshness probe.

Per reference_open_meteo_regional_fallback.md, the historical-forecast-api silently
falls back to global on 90d backfills for icon_d2 / icon_eu / ncep_hrrr_conus.
This probe checks whether the same fallback happens on FRESH intraday calls
(today / today+1 / today+2), which is the access pattern Phase2-01 would use.

Methodology:
  For each (city, regional_model, global_model) target:
    1. Fetch live-forecast endpoint with regional model
    2. Fetch live-forecast endpoint with global model
    3. Fetch historical-forecast-api with regional model
    4. Fetch historical-forecast-api with global model
  Compare:
    - live-regional vs live-global  (proves regional-distinct on live endpoint)
    - hist-regional vs hist-global   (the real question — does hist fall back?)
    - hist-regional vs live-regional (does hist serve same regional data as live?)

Positive control: arome_france (Paris) — known to be genuinely distinct per memory.
Baseline: Tokyo with icon_global vs icon_global (sanity check, should be identical).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

LIVE = "https://api.open-meteo.com/v1/forecast"
HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# (city, lat, lon, regional_model, global_model, note)
TARGETS = [
    ("Munich",  48.1351,  11.5820, "icon_d2",          "icon_global",   "ICON-D2 2km Germany regional"),
    ("Paris",   48.8566,   2.3522, "icon_eu",          "icon_global",   "ICON-EU 7km Europe regional"),
    ("Paris",   48.8566,   2.3522, "arome_france",     "icon_global",   "AROME positive control (memory says genuinely distinct)"),
    ("Chicago", 41.8781, -87.6298, "ncep_hrrr_conus",  "gfs_seamless",  "HRRR 3km CONUS regional"),
    ("Tokyo",   35.6762, 139.6503, "icon_global",      "icon_global",   "Baseline — same model both sides, expect identical"),
]


def fetch(url: str, lat: float, lon: float, model: str, start: str, end: str) -> dict | None:
    params = {
        "latitude":  lat,
        "longitude": lon,
        "daily":     "temperature_2m_max",
        "start_date": start,
        "end_date":   end,
        "timezone":   "auto",
        "models":     model,
        "temperature_unit": "celsius",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}", "_body": r.text[:300]}
        return r.json()
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def temps(payload: dict | None) -> list[float] | None:
    if not payload or "_error" in payload:
        return None
    daily = payload.get("daily", {}) or {}
    raw = daily.get("temperature_2m_max", []) or []
    return [float(t) for t in raw if t is not None]


def diff(a: list[float] | None, b: list[float] | None) -> tuple[bool, float | None]:
    if a is None or b is None or len(a) != len(b) or len(a) == 0:
        return (False, None)
    if a == b:
        return (True, 0.0)
    return (False, max(abs(x - y) for x, y in zip(a, b)))


def main() -> int:
    today = date.today()
    start = today.isoformat()
    end   = (today + timedelta(days=2)).isoformat()
    print(f"\nProbe window: {start} -> {end} (today + 2)\n")

    results = []
    for city, lat, lon, regional, global_m, note in TARGETS:
        print(f"=== {city} | {regional} vs {global_m} | {note}")

        live_r = fetch(LIVE, lat, lon, regional, start, end)
        time.sleep(0.3)
        live_g = fetch(LIVE, lat, lon, global_m, start, end)
        time.sleep(0.3)
        hist_r = fetch(HIST, lat, lon, regional, start, end)
        time.sleep(0.3)
        hist_g = fetch(HIST, lat, lon, global_m, start, end)
        time.sleep(0.3)

        lr, lg, hr, hg = temps(live_r), temps(live_g), temps(hist_r), temps(hist_g)

        live_match,        live_max_diff        = diff(lr, lg)
        hist_match,        hist_max_diff        = diff(hr, hg)
        live_vs_hist_reg,  live_vs_hist_reg_max = diff(lr, hr)

        row = {
            "city": city,
            "regional_model": regional,
            "global_model":   global_m,
            "note": note,
            "live_regional_temps": lr,
            "live_global_temps":   lg,
            "hist_regional_temps": hr,
            "hist_global_temps":   hg,
            "live_regional_eq_global":     live_match,
            "live_regional_vs_global_max": live_max_diff,
            "hist_regional_eq_global":     hist_match,
            "hist_regional_vs_global_max": hist_max_diff,
            "live_regional_eq_hist_regional":     live_vs_hist_reg,
            "live_regional_vs_hist_regional_max": live_vs_hist_reg_max,
            "errors": {
                "live_regional": live_r.get("_error") if isinstance(live_r, dict) and "_error" in live_r else None,
                "live_global":   live_g.get("_error") if isinstance(live_g, dict) and "_error" in live_g else None,
                "hist_regional": hist_r.get("_error") if isinstance(hist_r, dict) and "_error" in hist_r else None,
                "hist_global":   hist_g.get("_error") if isinstance(hist_g, dict) and "_error" in hist_g else None,
            },
        }
        results.append(row)

        print(f"  live  reg: {lr}")
        print(f"  live  glb: {lg}")
        print(f"  hist  reg: {hr}")
        print(f"  hist  glb: {hg}")
        print(f"  LIVE regional==global?  {live_match}  (max d {live_max_diff})")
        print(f"  HIST regional==global?  {hist_match}  (max d {hist_max_diff})  <-- THE QUESTION")
        print(f"  LIVE-reg == HIST-reg?   {live_vs_hist_reg}  (max d {live_vs_hist_reg_max})")
        if row["errors"]["live_regional"] or row["errors"]["live_global"] or row["errors"]["hist_regional"] or row["errors"]["hist_global"]:
            print(f"  errors: {row['errors']}")
        print()

    out = Path("tasks/findings/2026-05-08_om_hist_probe_raw.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nRaw results -> {out}")

    print("\n=== INTERPRETATION GUIDE ===")
    print("  HIST regional==global == True  -> historical-forecast-api falls back to global. Don't use it for Phase2-01; call live-forecast endpoint instead.")
    print("  HIST regional==global == False -> historical-forecast-api serves real regional data. Safe to use.")
    print("  AROME (positive control) should show False on HIST. If it shows True, the entire historical-forecast-api regional model surface is broken.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
