"""
METAR Observer — Phase 1
─────────────────────────
Batched METAR fetch from aviationweather.gov (18 cities) and HKO Open Data
(Hong Kong). Called once per poll tick from weather_bot.run_exit_pass() to
update in-memory MetarState for every configured city, write new readings to
DB, and pair each observation with the current ensemble snapshot.

All flags default off in config.py. Merge is zero-behavior-change.
Feature activation: set METAR_ENABLED=true in the environment.
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import requests

import db
from config import WEATHER

# ── City → ICAO / source / timezone table ────────────────────────────────────
# Source = "avwx" → aviationweather.gov batch API
#        = "hko"  → HKO Open Data API (Hong Kong only)
CITY_RESOLVERS: dict[str, dict] = {
    "paris":          {"icao": "LFPG", "src": "avwx", "tz": "Europe/Paris"},
    "london":         {"icao": "EGLC", "src": "avwx", "tz": "Europe/London"},
    "new york city":  {"icao": "KLGA", "src": "avwx", "tz": "America/New_York"},
    "new york":       {"icao": "KLGA", "src": "avwx", "tz": "America/New_York"},
    "dallas":         {"icao": "KDAL", "src": "avwx", "tz": "America/Chicago"},
    "istanbul":       {"icao": "LTFM", "src": "avwx", "tz": "Europe/Istanbul"},
    "hong kong":      {"icao": "HKO",  "src": "hko",  "tz": "Asia/Hong_Kong"},
    "toronto":        {"icao": "CYYZ", "src": "avwx", "tz": "America/Toronto"},
    "seoul":          {"icao": "RKSI", "src": "avwx", "tz": "Asia/Seoul"},
    "buenos aires":   {"icao": "SAEZ", "src": "avwx", "tz": "America/Argentina/Buenos_Aires"},
    "chicago":        {"icao": "KORD", "src": "avwx", "tz": "America/Chicago"},
    "miami":          {"icao": "KMIA", "src": "avwx", "tz": "America/New_York"},
    "moscow":         {"icao": "UUWW", "src": "avwx", "tz": "Europe/Moscow"},
    "madrid":         {"icao": "LEMD", "src": "avwx", "tz": "Europe/Madrid"},
    "munich":         {"icao": "EDDM", "src": "avwx", "tz": "Europe/Berlin"},
    "wellington":     {"icao": "NZWN", "src": "avwx", "tz": "Pacific/Auckland"},
    "atlanta":        {"icao": "KATL", "src": "avwx", "tz": "America/New_York"},
    "sao paulo":      {"icao": "SBGR", "src": "avwx", "tz": "America/Sao_Paulo"},
    "tel aviv":       {"icao": "LLBG", "src": "avwx", "tz": "Asia/Jerusalem"},
    "lucknow":        {"icao": "VILK", "src": "avwx", "tz": "Asia/Kolkata"},
    "shanghai":       {"icao": "ZSPD", "src": "avwx", "tz": "Asia/Shanghai"},
    "beijing":        {"icao": "ZBAA", "src": "avwx", "tz": "Asia/Shanghai"},
}

# ── Module-level config (read once at import to avoid per-call dict lookup) ──
_STALE_TTL_SEC       = WEATHER.get("metar_stale_ttl_sec",        300)
_PLAUSIBILITY_DELTA  = WEATHER.get("metar_plausibility_delta_c", 5.0)
_AVWX_URL            = WEATHER.get("metar_avwx_url",             "https://aviationweather.gov/api/data/metar")
_HKO_URL             = WEATHER.get("metar_hko_url",              "https://data.weather.gov.hk/weatherAPI/opendata/weather.php")
_USER_AGENT          = WEATHER.get("metar_user_agent",           "tradebot0/1.0")
_RECORD_TO_DB        = WEATHER.get("metar_record_to_db",         True)


# ── Public data classes ───────────────────────────────────────────────────────

@dataclass
class MetarReading:
    icao:            str
    observed_at_utc: datetime
    temp_c:          float
    dewpoint_c:      float | None
    raw_report:      str
    source:          str          # "avwx" | "hko"


@dataclass
class MetarState:
    city:             str                   # lowercase canonical city name
    icao:             str
    max_today_c:      float | None          # running max within market's local day
    last_reading:     MetarReading | None
    last_fetched_at_utc: datetime | None
    is_stale:         bool                  # True when last fetch > _STALE_TTL_SEC ago
    pending_suspect:  MetarReading | None = field(default=None, repr=False)

    def delta_to_threshold(self, threshold_c: float) -> float | None:
        if self.max_today_c is None:
            return None
        return self.max_today_c - threshold_c

    def has_locked_yes(self, threshold_c: float) -> bool:
        return self.max_today_c is not None and self.max_today_c >= threshold_c


# ── Module-level state ────────────────────────────────────────────────────────

_STATE: dict[str, MetarState] = {}     # city_lc → MetarState
_LOCK = threading.Lock()               # protect _STATE from concurrent reads (dashboard)


# ── Public interface ──────────────────────────────────────────────────────────

def refresh_all(cities: Iterable[str] | None = None) -> dict[str, MetarState]:
    """
    Run one batched fetch cycle. Updates in-memory state and writes new readings
    to DB. Called from weather_bot.run_exit_pass() once per poll tick.
    Returns a copy of the full state dict after the update.
    """
    if cities is None:
        cities = list(CITY_RESOLVERS.keys())
    else:
        cities = [c.lower() for c in cities]

    now_utc = datetime.now(timezone.utc)

    # Separate avwx vs hko cities
    avwx_cities  = {c: CITY_RESOLVERS[c] for c in cities if c in CITY_RESOLVERS and CITY_RESOLVERS[c]["src"] == "avwx"}
    hko_cities   = {c: CITY_RESOLVERS[c] for c in cities if c in CITY_RESOLVERS and CITY_RESOLVERS[c]["src"] == "hko"}

    # Batch fetch avwx
    avwx_readings: dict[str, list[MetarReading]] = {}
    if avwx_cities:
        icao_list = list({v["icao"] for v in avwx_cities.values()})
        avwx_readings = _fetch_avwx_batch(icao_list)

    # Fetch hko
    hko_readings: dict[str, list[MetarReading]] = {}
    for city, meta in hko_cities.items():
        reading = _fetch_hko_current(meta["tz"])
        if reading:
            hko_readings[city] = [reading]

    # Process each city
    all_cities = dict(avwx_cities)
    all_cities.update(hko_cities)

    for city, meta in all_cities.items():
        icao = meta["icao"]
        tz   = meta["tz"]

        if meta["src"] == "avwx":
            new_readings = avwx_readings.get(icao, [])
        else:
            new_readings = hko_readings.get(city, [])

        _update_city_state(city, icao, tz, new_readings, now_utc)

    with _LOCK:
        return dict(_STATE)


def get_state(city: str) -> MetarState | None:
    """Read current in-memory state without triggering a fetch. Called per-trade in exit loop."""
    with _LOCK:
        return _STATE.get(city.lower())


def get_all_states() -> dict[str, MetarState]:
    """Used by the dashboard METAR widget."""
    with _LOCK:
        return dict(_STATE)


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch_avwx_batch(icaos: list[str]) -> dict[str, list[MetarReading]]:
    """
    GET aviationweather.gov/api/data/metar?ids=A,B,...&format=json&hours=36
    Returns {icao_upper: [MetarReading, ...] sorted oldest→newest}.
    On any error, returns {} so callers keep their last-good state.
    """
    try:
        resp = requests.get(
            _AVWX_URL,
            params={
                "ids":    ",".join(icaos),
                "format": "json",
                "hours":  36,
                "taf":    "false",
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [metar] avwx batch fetch failed: {e}")
        return {}

    result: dict[str, list[MetarReading]] = {}
    for item in data:
        try:
            icao = (item.get("icaoId") or item.get("stationId") or "").upper()
            if not icao:
                continue
            temp = item.get("temp")
            if temp is None:
                continue
            temp_c = float(temp)
            dewp = item.get("dewp")
            dewpoint_c = float(dewp) if dewp is not None else None
            obs_time_str = item.get("reportTime") or item.get("obsTime") or ""
            observed_at = _parse_metar_time(obs_time_str)
            if observed_at is None:
                continue
            raw = item.get("rawOb") or item.get("rawReport") or ""
            reading = MetarReading(
                icao=icao,
                observed_at_utc=observed_at,
                temp_c=temp_c,
                dewpoint_c=dewpoint_c,
                raw_report=raw,
                source="avwx",
            )
            result.setdefault(icao, []).append(reading)
        except Exception:
            continue

    # Sort each ICAO's readings oldest → newest
    for icao in result:
        result[icao].sort(key=lambda r: r.observed_at_utc)

    return result


def _fetch_hko_current(tz: str) -> MetarReading | None:
    """
    GET HKO rhrread endpoint; parse HKO HQ temperature.
    Returns a single MetarReading or None on any failure.
    """
    try:
        resp = requests.get(
            _HKO_URL,
            params={"dataType": "rhrread", "lang": "en"},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [metar] hko fetch failed: {e}")
        return None

    try:
        # HKO HQ temperature is in data["temperature"]["data"][0]
        temp_data = data.get("temperature", {}).get("data", [])
        hq_entry = next(
            (d for d in temp_data if "Hong Kong Observatory" in (d.get("place") or "")),
            temp_data[0] if temp_data else None,
        )
        if not hq_entry:
            return None

        temp_c = float(hq_entry["value"])
        update_time_str = data.get("updateTime") or ""
        observed_at = _parse_hko_time(update_time_str)
        if observed_at is None:
            observed_at = datetime.now(timezone.utc)

        return MetarReading(
            icao="HKO",
            observed_at_utc=observed_at,
            temp_c=temp_c,
            dewpoint_c=None,
            raw_report="",
            source="hko",
        )
    except Exception as e:
        print(f"  [metar] hko parse failed: {e}")
        return None


# ── State update ──────────────────────────────────────────────────────────────

def _update_city_state(
    city: str,
    icao: str,
    tz: str,
    new_readings: list[MetarReading],
    now_utc: datetime,
) -> None:
    """Apply new readings to the city's MetarState, updating max_today and writing to DB."""
    with _LOCK:
        prev_state = _STATE.get(city)

    prev_reading = prev_state.last_reading if prev_state else None
    pending      = prev_state.pending_suspect if prev_state else None
    max_today    = prev_state.max_today_c    if prev_state else None

    fetch_succeeded = bool(new_readings)

    for reading in new_readings:
        accepted, reason = _plausibility_gate(prev_reading, reading, _PLAUSIBILITY_DELTA)

        if accepted:
            # If there was a pending suspect, it's now cleared (current reading disagrees → drop it)
            pending = None
            # Advance max only for today's local-day readings
            today_max = _compute_max_today([reading], tz, now_utc)
            if today_max is not None:
                if max_today is None or today_max > max_today:
                    max_today = today_max
            prev_reading = reading

            if _RECORD_TO_DB:
                _record_to_db(city, reading)

        elif reason == "suspect-delta":
            if pending is not None:
                # Previous suspect gets a second reading to confirm or deny
                confirm, _ = _plausibility_gate(pending, reading, 2.0)
                if confirm:
                    # Both suspect and current confirmed — accept both
                    for r in (pending, reading):
                        today_max = _compute_max_today([r], tz, now_utc)
                        if today_max is not None:
                            if max_today is None or today_max > max_today:
                                max_today = today_max
                        if _RECORD_TO_DB:
                            _record_to_db(city, r)
                    pending = None
                    prev_reading = reading
                else:
                    # Suspect dropped as glitch; current reading becomes the new baseline
                    pending = None
                    prev_reading = reading
            else:
                # Park the suspect; wait for next reading
                pending = reading

        # "no-prior-reading" case: first-boot safety belt — buffer first reading as suspect
        elif reason == "no-prior-reading":
            pending = reading
            # Don't advance max_today yet — wait for confirmation

    # Determine staleness
    if fetch_succeeded:
        last_fetched = now_utc
    elif prev_state and prev_state.last_fetched_at_utc:
        last_fetched = prev_state.last_fetched_at_utc
    else:
        last_fetched = None

    age_sec = (now_utc - last_fetched).total_seconds() if last_fetched else float("inf")
    is_stale = age_sec > _STALE_TTL_SEC

    new_state = MetarState(
        city=city,
        icao=icao,
        max_today_c=max_today,
        last_reading=prev_reading,
        last_fetched_at_utc=last_fetched,
        is_stale=is_stale,
        pending_suspect=pending,
    )

    with _LOCK:
        _STATE[city] = new_state


# ── Plausibility gate ─────────────────────────────────────────────────────────

def _plausibility_gate(
    prev: MetarReading | None,
    new: MetarReading,
    delta_limit_c: float,
) -> tuple[bool, str]:
    """
    Returns (accept, reason).
    No prior reading  → (False, "no-prior-reading") — first-boot safety belt.
    |Δ| <= limit      → (True, "plausible")
    |Δ| > limit       → (False, "suspect-delta")
    """
    if prev is None:
        return False, "no-prior-reading"
    delta = abs(new.temp_c - prev.temp_c)
    if delta <= delta_limit_c:
        return True, "plausible"
    return False, "suspect-delta"


# ── Max-today computation ─────────────────────────────────────────────────────

def _compute_max_today(
    readings: list[MetarReading],
    tz: str,
    now_utc: datetime,
) -> float | None:
    """
    Return max temp_c from readings that fall within the current local calendar day.
    Uses zoneinfo (Python 3.9+) for DST-aware timezone conversion.
    Falls back to UTC±offset approximation if zoneinfo unavailable.
    Returns None if no qualifying readings.
    """
    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(tz)
        today_local = now_utc.astimezone(local_tz).date()
        temps = [
            r.temp_c for r in readings
            if r.observed_at_utc.astimezone(local_tz).date() == today_local
        ]
    except Exception:
        # Fallback: UTC date comparison (acceptable error for cities near midnight)
        today_utc = now_utc.date()
        temps = [r.temp_c for r in readings if r.observed_at_utc.date() == today_utc]

    return max(temps) if temps else None


# ── DB recording ──────────────────────────────────────────────────────────────

def _record_to_db(city: str, reading: MetarReading) -> None:
    """
    Pair with current ensemble snapshot and INSERT OR IGNORE into metar_observations.
    Never raises — DB errors log and continue.
    """
    try:
        snapshot = _get_ensemble_snapshot(city)
        now_iso  = datetime.now(timezone.utc).isoformat()
        row = {
            "city":                  city,
            "icao":                  reading.icao,
            "observed_at_utc":       reading.observed_at_utc.isoformat(),
            "observed_temp_c":       reading.temp_c,
            "dewpoint_c":            reading.dewpoint_c,
            "source":                reading.source,
            "raw_report":            reading.raw_report or None,
            "ensemble_point_mean_c": snapshot.get("point_mean_c") if snapshot else None,
            "ensemble_p05_c":        snapshot.get("p05_c")        if snapshot else None,
            "ensemble_p50_c":        snapshot.get("p50_c")        if snapshot else None,
            "ensemble_p95_c":        snapshot.get("p95_c")        if snapshot else None,
            "forecast_run_at_utc":   snapshot.get("forecast_run_at_utc") if snapshot else None,
            "fetched_at_utc":        now_iso,
        }
        db.record_metar_observation(row)
    except Exception as e:
        print(f"  [metar] db record failed for {city}: {e}")


def _get_ensemble_snapshot(city: str) -> dict | None:
    """
    Pull the current ensemble snapshot from layer3_weather's in-memory cache.
    Import is deferred to avoid circular import at module load time.
    Returns None if no cached forecast exists yet.
    """
    try:
        from layers.layer3_weather import _cache, CITY_COORDS, find_ensemble_day
        import time
        entry = _cache.get(city)
        if not entry:
            return None
        ensemble = entry.get("ensemble", [])
        if not ensemble:
            return None
        # Use today's UTC date as a proxy for the market date
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        member_temps = find_ensemble_day(ensemble, today)
        if not member_temps or len(member_temps) < 3:
            return None
        sorted_t = sorted(member_temps)
        n = len(sorted_t)
        mean_c = sum(sorted_t) / n
        p05_c  = sorted_t[max(0, int(n * 0.05))]
        p50_c  = sorted_t[n // 2]
        p95_c  = sorted_t[min(n - 1, int(n * 0.95))]
        return {
            "point_mean_c":        round(mean_c, 2),
            "p05_c":               round(p05_c,  2),
            "p50_c":               round(p50_c,  2),
            "p95_c":               round(p95_c,  2),
            "forecast_run_at_utc": datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat(),
        }
    except Exception:
        return None


# ── Time parsers ──────────────────────────────────────────────────────────────

def _parse_metar_time(s: str) -> datetime | None:
    """Parse aviationweather reportTime strings like '2026-04-22 14:00:00' or ISO."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_hko_time(s: str) -> datetime | None:
    """Parse HKO updateTime like '20260422150000+0800'."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        # '20260422150000+0800'
        if len(s) >= 14:
            dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            return dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None
