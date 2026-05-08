import math
import re
import time
from datetime import datetime, timezone

from layers.base import BaseLayer, LayerEstimate
from markets.open_meteo import get_daily_forecast, get_ensemble_forecasts
from config import WEATHER
import db

# ── City coordinate database ──────────────────────────────────────────────────
# Maps lowercase city name → lat/lon/timezone for Open-Meteo lookups.
# Covers all cities commonly traded on Polymarket weather markets.
CITY_COORDS: dict[str, dict] = {
    "wellington":       {"lat": -41.2865, "lon":  174.7762, "tz": "Pacific/Auckland"},
    "shenzhen":         {"lat":  22.5431, "lon":  114.0579, "tz": "Asia/Shanghai"},
    "shanghai":         {"lat":  31.2304, "lon":  121.4737, "tz": "Asia/Shanghai"},
    "beijing":          {"lat":  39.9042, "lon":  116.4074, "tz": "Asia/Shanghai"},
    "singapore":        {"lat":   1.3521, "lon":  103.8198, "tz": "Asia/Singapore"},
    "tokyo":            {"lat":  35.6762, "lon":  139.6503, "tz": "Asia/Tokyo"},
    "london":           {"lat":  51.5074, "lon":   -0.1278, "tz": "Europe/London"},
    "los angeles":      {"lat":  34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},
    "atlanta":          {"lat":  33.7490, "lon":  -84.3880, "tz": "America/New_York"},
    "chengdu":          {"lat":  30.5728, "lon":  104.0668, "tz": "Asia/Shanghai"},
    "wuhan":            {"lat":  30.5928, "lon":  114.3055, "tz": "Asia/Shanghai"},
    "chicago":          {"lat":  41.8781, "lon":  -87.6298, "tz": "America/Chicago"},
    "houston":          {"lat":  29.7604, "lon":  -95.3698, "tz": "America/Chicago"},
    "hong kong":        {"lat":  22.3193, "lon":  114.1694, "tz": "Asia/Hong_Kong"},
    "buenos aires":     {"lat": -34.6037, "lon":  -58.3816, "tz": "America/Argentina/Buenos_Aires"},
    "ankara":           {"lat":  39.9334, "lon":   32.8597, "tz": "Europe/Istanbul"},
    "sao paulo":        {"lat": -23.5505, "lon":  -46.6333, "tz": "America/Sao_Paulo"},
    "toronto":          {"lat":  43.6532, "lon":  -79.3832, "tz": "America/Toronto"},
    "new york city":    {"lat":  40.7128, "lon":  -74.0060, "tz": "America/New_York"},
    "new york":         {"lat":  40.7128, "lon":  -74.0060, "tz": "America/New_York"},
    "seattle":          {"lat":  47.6062, "lon": -122.3321, "tz": "America/Los_Angeles"},
    "dallas":           {"lat":  32.7767, "lon":  -96.7970, "tz": "America/Chicago"},
    "munich":           {"lat":  48.1351, "lon":   11.5820, "tz": "Europe/Berlin"},
    "paris":            {"lat":  48.8566, "lon":    2.3522, "tz": "Europe/Paris"},
    "madrid":           {"lat":  40.4168, "lon":   -3.7038, "tz": "Europe/Madrid"},
    "lucknow":          {"lat":  26.8467, "lon":   80.9462, "tz": "Asia/Kolkata"},
    "miami":            {"lat":  25.7617, "lon":  -80.1918, "tz": "America/New_York"},
    "denver":           {"lat":  39.7392, "lon": -104.9903, "tz": "America/Denver"},
    "phoenix":          {"lat":  33.4484, "lon": -112.0740, "tz": "America/Phoenix"},
    "moscow":           {"lat":  55.7558, "lon":   37.6173, "tz": "Europe/Moscow"},
    "berlin":           {"lat":  52.5200, "lon":   13.4050, "tz": "Europe/Berlin"},
    "rome":             {"lat":  41.9028, "lon":   12.4964, "tz": "Europe/Rome"},
    "amsterdam":        {"lat":  52.3676, "lon":    4.9041, "tz": "Europe/Amsterdam"},
    "dubai":            {"lat":  25.2048, "lon":   55.2708, "tz": "Asia/Dubai"},
    "mumbai":           {"lat":  19.0760, "lon":   72.8777, "tz": "Asia/Kolkata"},
    "delhi":            {"lat":  28.7041, "lon":   77.1025, "tz": "Asia/Kolkata"},
    "sydney":           {"lat": -33.8688, "lon":  151.2093, "tz": "Australia/Sydney"},
    "melbourne":        {"lat": -37.8136, "lon":  144.9631, "tz": "Australia/Melbourne"},
    "bangkok":          {"lat":  13.7563, "lon":  100.5018, "tz": "Asia/Bangkok"},
    "seoul":            {"lat":  37.5665, "lon":  126.9780, "tz": "Asia/Seoul"},
    "taipei":           {"lat":  25.0330, "lon":  121.5654, "tz": "Asia/Taipei"},
    "jakarta":          {"lat":  -6.2088, "lon":  106.8456, "tz": "Asia/Jakarta"},
    "istanbul":         {"lat":  41.0082, "lon":   28.9784, "tz": "Europe/Istanbul"},
    "cairo":            {"lat":  30.0444, "lon":   31.2357, "tz": "Africa/Cairo"},
    "lagos":            {"lat":   6.5244, "lon":    3.3792, "tz": "Africa/Lagos"},
    "nairobi":          {"lat":  -1.2921, "lon":   36.8219, "tz": "Africa/Nairobi"},
    "johannesburg":     {"lat": -26.2041, "lon":   28.0473, "tz": "Africa/Johannesburg"},
    "mexico city":      {"lat":  19.4326, "lon":  -99.1332, "tz": "America/Mexico_City"},
    "bogota":           {"lat":   4.7110, "lon":  -74.0721, "tz": "America/Bogota"},
    "lima":             {"lat": -12.0464, "lon":  -77.0428, "tz": "America/Lima"},
    "santiago":         {"lat": -33.4489, "lon":  -70.6693, "tz": "America/Santiago"},
    "karachi":          {"lat":  24.8607, "lon":   67.0011, "tz": "Asia/Karachi"},
    "dhaka":            {"lat":  23.8103, "lon":   90.4125, "tz": "Asia/Dhaka"},
    "kolkata":          {"lat":  22.5726, "lon":   88.3639, "tz": "Asia/Kolkata"},
    "chennai":          {"lat":  13.0827, "lon":   80.2707, "tz": "Asia/Kolkata"},
    "tehran":           {"lat":  35.6892, "lon":   51.3890, "tz": "Asia/Tehran"},
    "riyadh":           {"lat":  24.7136, "lon":   46.6753, "tz": "Asia/Riyadh"},
    "tel aviv":         {"lat":  32.0853, "lon":   34.7818, "tz": "Asia/Jerusalem"},
    "athens":           {"lat":  37.9838, "lon":   23.7275, "tz": "Europe/Athens"},
    "warsaw":           {"lat":  52.2297, "lon":   21.0122, "tz": "Europe/Warsaw"},
    "vienna":           {"lat":  48.2082, "lon":   16.3738, "tz": "Europe/Vienna"},
    "brussels":         {"lat":  50.8503, "lon":    4.3517, "tz": "Europe/Brussels"},
    "stockholm":        {"lat":  59.3293, "lon":   18.0686, "tz": "Europe/Stockholm"},
    "oslo":             {"lat":  59.9139, "lon":   10.7522, "tz": "Europe/Oslo"},
    "copenhagen":       {"lat":  55.6761, "lon":   12.5683, "tz": "Europe/Copenhagen"},
    "helsinki":         {"lat":  60.1699, "lon":   24.9384, "tz": "Europe/Helsinki"},
    "zurich":           {"lat":  47.3769, "lon":    8.5417, "tz": "Europe/Zurich"},
    "lisbon":           {"lat":  38.7223, "lon":   -9.1393, "tz": "Europe/Lisbon"},
    "barcelona":        {"lat":  41.3851, "lon":    2.1734, "tz": "Europe/Madrid"},
    "milan":            {"lat":  45.4654, "lon":    9.1859, "tz": "Europe/Rome"},
    "montreal":         {"lat":  45.5017, "lon":  -73.5673, "tz": "America/Toronto"},
    "vancouver":        {"lat":  49.2827, "lon": -123.1207, "tz": "America/Vancouver"},
    "san francisco":    {"lat":  37.7749, "lon": -122.4194, "tz": "America/Los_Angeles"},
    "boston":           {"lat":  42.3601, "lon":  -71.0589, "tz": "America/New_York"},
    "washington":       {"lat":  38.9072, "lon":  -77.0369, "tz": "America/New_York"},
    "philadelphia":     {"lat":  39.9526, "lon":  -75.1652, "tz": "America/New_York"},
    "las vegas":        {"lat":  36.1699, "lon": -115.1398, "tz": "America/Los_Angeles"},
    "san diego":        {"lat":  32.7157, "lon": -117.1611, "tz": "America/Los_Angeles"},
    "austin":           {"lat":  30.2672, "lon":  -97.7431, "tz": "America/Chicago"},
    "tampa":            {"lat":  27.9506, "lon":  -82.4572, "tz": "America/New_York"},
    "new orleans":      {"lat":  29.9511, "lon":  -90.0715, "tz": "America/Chicago"},
    "portland":         {"lat":  45.5051, "lon": -122.6750, "tz": "America/Los_Angeles"},
    "minneapolis":      {"lat":  44.9778, "lon":  -93.2650, "tz": "America/Chicago"},
    "salt lake city":   {"lat":  40.7608, "lon": -111.8910, "tz": "America/Denver"},
    "kansas city":      {"lat":  39.0997, "lon":  -94.5786, "tz": "America/Chicago"},
    "ottawa":           {"lat":  45.4215, "lon":  -75.6972, "tz": "America/Toronto"},
    "calgary":          {"lat":  51.0447, "lon": -114.0719, "tz": "America/Edmonton"},
    "edinburgh":        {"lat":  55.9533, "lon":   -3.1883, "tz": "Europe/London"},
    "manchester":       {"lat":  53.4808, "lon":   -2.2426, "tz": "Europe/London"},
    "birmingham":       {"lat":  52.4862, "lon":   -1.8904, "tz": "Europe/London"},
    "glasgow":          {"lat":  55.8642, "lon":   -4.2518, "tz": "Europe/London"},
    "brussels":         {"lat":  50.8503, "lon":    4.3517, "tz": "Europe/Brussels"},
    "prague":           {"lat":  50.0755, "lon":   14.4378, "tz": "Europe/Prague"},
    "budapest":         {"lat":  47.4979, "lon":   19.0402, "tz": "Europe/Budapest"},
    "bucharest":        {"lat":  44.4268, "lon":   26.1025, "tz": "Europe/Bucharest"},
    "sofia":            {"lat":  42.6977, "lon":   23.3219, "tz": "Europe/Sofia"},
    "zagreb":           {"lat":  45.8150, "lon":   15.9819, "tz": "Europe/Zagreb"},
    "belgrade":         {"lat":  44.8176, "lon":   20.4633, "tz": "Europe/Belgrade"},
    "kyiv":             {"lat":  50.4501, "lon":   30.5234, "tz": "Europe/Kiev"},
    "minsk":            {"lat":  53.9006, "lon":   27.5590, "tz": "Europe/Minsk"},
    "riga":             {"lat":  56.9460, "lon":   24.1059, "tz": "Europe/Riga"},
    "vilnius":          {"lat":  54.6872, "lon":   25.2797, "tz": "Europe/Vilnius"},
    "tallinn":          {"lat":  59.4370, "lon":   24.7536, "tz": "Europe/Tallinn"},
}

# ── Parsers ───────────────────────────────────────────────────────────────────

# "13°C or below" / "17°C or higher"
_THRESH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*°?\s*([CcFf])\s+(?:or\s+)?(below|above|higher|lower)",
    re.IGNORECASE,
)

# "between 82-83°F" or "between 82 and 83°F"
_RANGE_RE = re.compile(
    r"between\s+(\d+(?:\.\d+)?)[^\d]+(\d+(?:\.\d+)?)\s*°?\s*([CcFf])",
    re.IGNORECASE,
)

# "be 13°C on ..." (exact, no "or" qualifier)
_EXACT_RE = re.compile(
    r"be\s+(\d+(?:\.\d+)?)\s*°\s*([CcFf])(?!\s*(?:or|and|\d))",
    re.IGNORECASE,
)

_CITY_RE = re.compile(
    r"\bin\s+([A-Za-z][A-Za-z\s\-\.\']+?)(?=\s+(?:be|reach|on|will|exceeds?|:|\?|,|$|\s*\d))",
    re.IGNORECASE,
)


def parse_weather_market(question: str) -> dict | None:
    """
    Parse a Polymarket temperature market question. Handles three formats:
      - "be 13°C or below/higher"  → type "threshold"
      - "be between 82-83°F"       → type "range"
      - "be 13°C on ..."           → type "exact"

    Returns a dict with city_raw, unit, market_type, and type-specific fields,
    or None if parsing fails.
    """
    city_match = _CITY_RE.search(question)
    if not city_match:
        return None

    city_raw = city_match.group(1).strip().lower()
    city_raw = re.sub(r"\s+(be|to|will|temperature|temp)\s*$", "", city_raw, flags=re.IGNORECASE).strip()

    # 1. Threshold ("or below" / "or higher")
    thresh = _THRESH_RE.search(question)
    if thresh:
        return {
            "city_raw":    city_raw,
            "unit":        thresh.group(2).upper(),
            "market_type": "threshold",
            "threshold":   float(thresh.group(1)),
            "direction":   "above" if thresh.group(3).lower() in ("above", "higher") else "below",
        }

    # 2. Range ("between X-Y°C")
    rng = _RANGE_RE.search(question)
    if rng:
        return {
            "city_raw":    city_raw,
            "unit":        rng.group(3).upper(),
            "market_type": "range",
            "low":         float(rng.group(1)),
            "high":        float(rng.group(2)),
        }

    # 3. Exact ("be 13°C on …")
    exact = _EXACT_RE.search(question)
    if exact:
        return {
            "city_raw":    city_raw,
            "unit":        exact.group(2).upper(),
            "market_type": "exact",
            "exact":       float(exact.group(1)),
        }

    return None


def find_city(city_raw: str) -> tuple[str, dict] | None:
    """Return (city_key, coords) for city_raw. Tries exact then substring match."""
    key = city_raw.lower().strip()
    if key in CITY_COORDS:
        return key, CITY_COORDS[key]
    for k, v in CITY_COORDS.items():
        if k in key or key in k:
            return k, v
    return None


# ── Forecast cache ────────────────────────────────────────────────────────────

_SCAN_TTL     = WEATHER.get("cache_ttl_minutes",           90) * 60   # scan-only cities
_POSITION_TTL = WEATHER.get("position_cache_ttl_minutes", 25) * 60   # cities with open trade
_DECISION_TTL = WEATHER.get("decision_cache_ttl_minutes", 180) * 60

# Pre-populate from DB so restarts don't bust the cache and cause burst API calls.
# Entries older than SCAN_TTL will still be refreshed on first access — they just
# won't all fire simultaneously on startup.
_cache: dict[str, dict] = db.load_forecast_cache()


def get_city_forecast(city_key: str, coords: dict, max_age: float | None = None) -> dict | None:
    """
    Return cached forecast dict for a city, fetching from Open-Meteo if stale.
    max_age: seconds before cache is considered stale. Defaults to scan TTL (60 min).
             Pass _DECISION_TTL to enforce the 3-hour freshness requirement for trades.

    If the ensemble API is rate-limited, the stale cache entry (if any) is returned
    as-is so the scanner can keep running on the last-known forecast. If no stale
    entry exists, the city is cached with an empty ensemble and the sigmoid fallback
    is used until the rate limit clears.
    """
    ttl   = max_age if max_age is not None else _SCAN_TTL
    now   = time.time()
    entry = _cache.get(city_key)
    if entry and (now - entry["ts"]) < ttl:
        return entry

    forecast = get_daily_forecast(coords["lat"], coords["lon"], coords["tz"])
    ensemble = get_ensemble_forecasts(coords["lat"], coords["lon"], coords["tz"])

    if not forecast:
        # Point forecast failed — return stale entry if available rather than None
        if entry:
            return entry
        return None

    # If ensemble came back empty (rate-limited or API down) and we have a stale
    # entry with real ensemble data, keep using the stale ensemble rather than
    # overwriting with an empty list.
    if not ensemble and entry and entry.get("ensemble"):
        ensemble = entry["ensemble"]

    entry = {"ts": now, "forecast": forecast, "ensemble": ensemble}
    _cache[city_key] = entry
    db.save_forecast_cache(city_key, now, forecast, ensemble)
    return entry


# ── Utilities ─────────────────────────────────────────────────────────────────

def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def find_forecast_day(forecast: list[dict], target_date: str, key: str):
    """Return value for target_date, or None if target_date is outside the forecast window.

    Previously fell back to forecast[1] / forecast[0] when target_date was missing.
    That silently returned wrong-date data once the target rolled out of Open-Meteo's
    window (e.g. a market closing today read tomorrow's forecast in the market's tz),
    triggering false exit signals. Callers must treat None as "no signal".
    """
    for day in forecast:
        if day.get("date") == target_date:
            return day.get(key)
    return None


def find_ensemble_day(ensemble: list[dict], target_date: str) -> list[float] | None:
    """Return ensemble member temps for target_date, or None if outside the window.

    See find_forecast_day for why no fallback — same silent wrong-date bug applies.
    """
    for day in ensemble:
        if day.get("date") == target_date:
            return day.get("member_temps")
    return None


# ── Shared threshold parser (used by weather_exit and metar_observer) ─────────

_COMPACT_THRESH_RE = re.compile(
    r"^([<>]=?)\s*(\d+(?:\.\d+)?)\s*([CcFf])$",
    re.IGNORECASE,
)
_BARE_VALUE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*([CcFf])$",
    re.IGNORECASE,
)


def parse_threshold_c(s: str) -> tuple[str, float] | None:
    """
    Parse a compact threshold string stored in the trades.threshold column.
    Formats: '>=9C', '<=15C', '>=66F', '<=21C', '30C', '66F'.
    Returns (operator, celsius_value) where operator is '>=' or '<='.
    Returns None on any parse failure.
    """
    if not s:
        return None
    s = s.strip()
    m = _COMPACT_THRESH_RE.match(s)
    if m:
        op     = m.group(1)
        value  = float(m.group(2))
        unit   = m.group(3).upper()
        temp_c = value if unit == "C" else f_to_c(value)
        # Normalise operator to two-char form
        if op in (">=", ">"):
            return ">=", temp_c
        if op in ("<=", "<"):
            return "<=", temp_c
    # Bare value without operator (exact/range market stored without op) — skip for METAR trigger
    if _BARE_VALUE_RE.match(s):
        return None
    return None


# ── WeatherLayer ──────────────────────────────────────────────────────────────

class WeatherLayer(BaseLayer):
    """
    Estimates YES probability for Polymarket temperature threshold markets
    using Open-Meteo point forecasts and a 71-member multi-model ensemble
    (ICON 40 + GFS 31).

    Decision layer behaviour:
    - Only evaluates the top N markets by 24h volume (configurable via WEATHER["top_n_markets"]).
    - Always fetches forecast data fresh relative to the ICON 3-hour update cycle
      (WEATHER["decision_cache_ttl_minutes"]) so trade signals reflect the latest model run.
    - The scanner (scan()) still uses the lighter 30-min cache for discovery/display.
    """

    def __init__(self):
        self._top_market_ids: set[str] = set()   # populated by refresh()

    def _fetch_top_market_ids(self) -> set[str]:
        """Fetch top N weather markets by 24h volume from Polymarket."""
        import json, requests as _req
        top_n   = WEATHER.get("top_n_markets", 15)
        session = _req.Session()
        session.headers.update({"User-Agent": "weather-bot/1.0"})
        ids: list[tuple[float, str]] = []   # (volume24hr, market_id)

        try:
            for page in range(25):
                resp = session.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "limit":     200,
                        "offset":    page * 200,
                        "order":     "volume24hr",
                        "ascending": "false",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break

                for m in batch:
                    if "highest-temperature" not in (m.get("slug") or "").lower():
                        continue
                    vol = float(m.get("volume24hr") or 0)
                    mid = m.get("conditionId") or m.get("id", "")
                    ids.append((vol, mid))

                if len(ids) >= top_n:
                    break
                if batch and float(batch[-1].get("volume24hr") or 0) < 10:
                    break
                if len(batch) < 200:
                    break
        except Exception as e:
            print(f"[weather] top-N refresh failed: {e}")

        ids.sort(reverse=True)
        top_ids = {mid for _, mid in ids[:top_n]}
        print(f"[weather] top-{top_n} market IDs refreshed ({len(top_ids)} loaded)")
        return top_ids

    def can_handle(self, market: dict) -> bool:
        """Only handle parseable weather markets that are in the top-N by volume."""
        parsed = parse_weather_market(market.get("question", ""))
        if not parsed:
            return False
        if find_city(parsed["city_raw"]) is None:
            return False
        # If top-N list is populated, restrict to those markets only
        if self._top_market_ids:
            mid = market.get("id") or market.get("conditionId", "")
            return mid in self._top_market_ids
        return True

    def refresh(self):
        """Refresh top-N market list. Does NOT clear the forecast cache — cities
        expire naturally via TTL so refreshes don't trigger a burst of API calls."""
        self._top_market_ids = self._fetch_top_market_ids()

    def estimate(self, market: dict) -> LayerEstimate | None:
        """
        Used by the bot's decision pipeline. Fetches fresh forecast data
        (respects decision_cache_ttl_minutes, not the lighter scan TTL).
        """
        scan_data = self._compute(market, max_age=_DECISION_TTL)
        if scan_data is None:
            return None

        if scan_data["edge_prob"] < WEATHER.get("min_edge", 0.05):
            return None

        return LayerEstimate(
            probability=scan_data["probability"],
            confidence=scan_data["confidence"],
            source="weather_forecast",
            raw_data=scan_data,
        )

    def scan(self, market: dict) -> dict | None:
        """
        Return all forecast data for a market regardless of edge size.
        Uses the lighter 30-min scan cache. Used by the scanner for display.
        """
        return self._compute(market, max_age=_SCAN_TTL)

    def _compute(self, market: dict, max_age: float) -> dict | None:
        """
        Core computation shared by scan() and estimate().
        max_age controls how fresh the cached forecast must be.
        """
        parsed = parse_weather_market(market.get("question", ""))
        if not parsed:
            return None

        city_result = find_city(parsed["city_raw"])
        if not city_result:
            return None

        city_key, coords = city_result

        # Cities with an open position get a shorter TTL so exit signals stay fresh.
        # Only override when the caller passed the scan TTL — the decision layer's
        # 3-hour TTL is intentionally relaxed and should not be tightened here.
        effective_max_age = max_age
        if max_age >= _SCAN_TTL and city_key in db.get_open_cities():
            effective_max_age = _POSITION_TTL

        cache = get_city_forecast(city_key, coords, max_age=effective_max_age)
        if not cache:
            return None

        market_date  = (market.get("end_date") or "")[:10]
        point_temp_c = find_forecast_day(cache["forecast"], market_date, "temp_max_c")
        ensemble_day = find_ensemble_day(cache["ensemble"], market_date)

        if point_temp_c is None:
            return None

        unit         = parsed["unit"]
        market_type  = parsed["market_type"]
        market_price = market["price"]

        # Convert forecast to native unit for display
        point_display = c_to_f(point_temp_c) if unit == "F" else point_temp_c

        # ── Build condition function and display strings per market type ──────
        if market_type == "threshold":
            threshold_c = parsed["threshold"] if unit == "C" else f_to_c(parsed["threshold"])
            direction   = parsed["direction"]
            if direction == "below":
                condition     = lambda t: t <= threshold_c
                target_str    = f"<={parsed['threshold']:.0f}{unit}"
                delta_display = -(point_display - parsed["threshold"])
                # positive delta = forecast below threshold = YES favored
            else:
                condition     = lambda t: t >= threshold_c
                target_str    = f">={parsed['threshold']:.0f}{unit}"
                delta_display = point_display - parsed["threshold"]
                # positive delta = forecast above threshold = YES favored

        elif market_type == "range":
            low_c  = parsed["low"]  if unit == "C" else f_to_c(parsed["low"])
            high_c = parsed["high"] if unit == "C" else f_to_c(parsed["high"])
            condition     = lambda t: low_c <= t <= high_c
            target_str    = f"{parsed['low']:.0f}-{parsed['high']:.0f}{unit}"
            mid_native    = (parsed["low"] + parsed["high"]) / 2
            delta_display = point_display - mid_native
            direction     = "range"

        else:  # exact
            exact_c    = parsed["exact"] if unit == "C" else f_to_c(parsed["exact"])
            tolerance  = 0.5   # +/-0.5 deg C
            condition     = lambda t: abs(t - exact_c) <= tolerance
            target_str    = f"={parsed['exact']:.0f}{unit}"
            delta_display = point_display - parsed["exact"]
            direction     = "exact"

        # ── Probability via ensemble or sigmoid fallback ──────────────────────
        ensemble_margin_c = None   # signed margin of ensemble median from threshold (Celsius)
        if ensemble_day and len(ensemble_day) >= 10:
            yes_count   = sum(1 for t in ensemble_day if condition(t))
            n           = len(ensemble_day)
            probability = yes_count / n
            agreement   = max(probability, 1 - probability)
            confidence  = min(0.90, 0.45 + (agreement - 0.50) * 1.60 + (n / 71) * 0.05)

            # Compute ensemble median margin from threshold (for coin-flip filter).
            # Positive = median is on the winning side of threshold; negative = losing side.
            if market_type == "threshold":
                sorted_temps = sorted(ensemble_day)
                median_temp  = sorted_temps[len(sorted_temps) // 2]
                if direction == "above":
                    ensemble_margin_c = median_temp - threshold_c
                else:
                    ensemble_margin_c = threshold_c - median_temp
        else:
            # Sigmoid on delta in Celsius
            delta_c = point_temp_c - (
                (parsed["threshold"] if unit == "C" else f_to_c(parsed["threshold"]))
                if market_type == "threshold"
                else ((low_c + high_c) / 2 if market_type == "range" else exact_c)
            )
            if market_type == "threshold" and direction == "below":
                delta_c = -delta_c
            probability = 1 / (1 + math.exp(-delta_c * 0.8))
            yes_count   = None
            n           = 0
            confidence  = 0.40

        edge_prob = abs(probability - market_price)
        edge_c    = abs(delta_display) if unit == "C" else abs(delta_display) * 5 / 9

        return {
            "city":              city_key,
            "unit":              unit,
            "market_type":       market_type,
            "direction":         direction,
            "target_str":        target_str,
            "point_temp_c":      point_temp_c,
            "point_display":     point_display,
            "delta_display":     delta_display,
            "edge_c":            edge_c,
            "ensemble_n":        n,
            "yes_ensemble":      yes_count,
            "ensemble_margin_c": ensemble_margin_c,
            "probability":       probability,
            "confidence":        confidence,
            "market_price":      market_price,
            "edge_prob":         edge_prob,
        }

    def get_cached_ensemble_snapshot(self, city: str) -> dict | None:
        """
        Return a snapshot dict for pairing with a METAR observation row:
          { "point_mean_c", "p05_c", "p50_c", "p95_c", "forecast_run_at_utc" }
        or None if no cached forecast exists for the city right now.
        Pure read — never triggers an Open-Meteo fetch.
        """
        import time as _time
        entry = _cache.get(city.lower())
        if not entry:
            return None
        ensemble = entry.get("ensemble", [])
        if not ensemble:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        member_temps = find_ensemble_day(ensemble, today)
        if not member_temps or len(member_temps) < 3:
            return None
        sorted_t = sorted(member_temps)
        n        = len(sorted_t)
        mean_c   = sum(sorted_t) / n
        p05_c    = sorted_t[max(0, int(n * 0.05))]
        p50_c    = sorted_t[n // 2]
        p95_c    = sorted_t[min(n - 1, int(n * 0.95))]
        return {
            "point_mean_c":        round(mean_c, 2),
            "p05_c":               round(p05_c,  2),
            "p50_c":               round(p50_c,  2),
            "p95_c":               round(p95_c,  2),
            "forecast_run_at_utc": datetime.fromtimestamp(entry["ts"], tz=timezone.utc).isoformat(),
        }
