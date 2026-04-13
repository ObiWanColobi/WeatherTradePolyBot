import time
import requests
from requests.exceptions import ConnectionError as ReqConnectionError, ReadTimeout, Timeout
import api_monitor

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"

_session = requests.Session()
_session.headers.update({"User-Agent": "weather-bot/1.0"})

_INTER_REQUEST_DELAY = 0.25      # seconds between ensemble API calls to avoid 429s
_RETRY_DELAYS        = [3, 8, 15] # backoff on 429 or connection error (3 attempts)

# Module-level telemetry for calibration scheduler gating
_last_429_ts: float = 0.0         # unix timestamp of most recent 429 from any endpoint
_last_ensemble_ts: float = 0.0    # unix timestamp of most recent ensemble API call


def get_last_429_ts() -> float:
    return _last_429_ts


def get_last_ensemble_ts() -> float:
    return _last_ensemble_ts


def get_daily_forecast(lat: float, lon: float, tz: str = "auto", days: int = 4) -> list[dict]:
    """
    Returns daily max temperature forecasts for the next `days` days.
    Each entry: {"date": "YYYY-MM-DD", "temp_max_c": float}
    Returns empty list on failure.
    """
    if api_monitor.get_breaker("open_meteo").state == "OPEN":
        return []

    for attempt in range(1 + len(_RETRY_DELAYS)):
        if attempt > 0:
            time.sleep(_RETRY_DELAYS[attempt - 1])
        try:
            resp = _session.get(
                FORECAST_API,
                params={
                    "latitude":         lat,
                    "longitude":        lon,
                    "daily":            "temperature_2m_max",
                    "timezone":         tz,
                    "forecast_days":    days,
                    "temperature_unit": "celsius",
                },
                timeout=10,
            )
            if resp.status_code in (502, 503):
                print(f"[open_meteo] point forecast failed ({lat},{lon}): "
                      f"{resp.status_code} Server Error — skipping retries")
                api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
                return []
            resp.raise_for_status()
            data  = resp.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            temps = daily.get("temperature_2m_max", [])
            api_monitor.get_breaker("open_meteo").record_success()
            return [
                {"date": d, "temp_max_c": float(t)}
                for d, t in zip(dates, temps)
                if t is not None
            ]
        except (ReqConnectionError, ReadTimeout, Timeout) as e:
            # Network-level failures indicate API is down — skip retries
            print(f"[open_meteo] point forecast failed ({lat},{lon}): {e}")
            api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
            return []
        except Exception as e:
            if attempt < len(_RETRY_DELAYS):
                continue
            print(f"[open_meteo] point forecast failed ({lat},{lon}): {e}")
            api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
    return []


_ENSEMBLE_MODELS = ["icon_seamless", "gfs025"]   # 40 + 31 = up to 71 members


def get_ensemble_forecasts(lat: float, lon: float, tz: str = "auto", days: int = 4) -> list[dict]:
    """
    Returns a multi-model ensemble combining ICON (40 members) and GFS (31 members)
    for up to 71 total daily max temps per day.

    Using two independent models captures both initial-condition uncertainty (within
    each model's ensemble) and model-structural uncertainty (between models), which
    improves forecast skill over a single-model ensemble.

    Each entry: {"date": "YYYY-MM-DD", "member_temps": [float, ...]}  (up to 71 values)
    Returns empty list on failure — point forecast is the fallback.
    """
    if api_monitor.get_breaker("open_meteo").state == "OPEN":
        return []

    combined: dict[str, list[float]] = {}   # date → accumulated member temps

    for model in _ENSEMBLE_MODELS:
        params = {
            "latitude":      lat,
            "longitude":     lon,
            "daily":         "temperature_2m_max",
            "models":        model,
            "timezone":      tz,
            "forecast_days": days,
        }
        data = None
        for attempt in range(1 + len(_RETRY_DELAYS)):
            if attempt > 0:
                time.sleep(_RETRY_DELAYS[attempt - 1])
            try:
                global _last_ensemble_ts
                _last_ensemble_ts = time.time()
                resp = _session.get(ENSEMBLE_API, params=params, timeout=15)
                if resp.status_code == 429:
                    global _last_429_ts
                    _last_429_ts = time.time()
                    print(f"[open_meteo] ensemble rate-limited model={model} ({lat},{lon}): skipping (will use stale/sigmoid)")
                    break   # no retries on 429 — back off and let TTL serve stale cache
                if resp.status_code in (502, 503):
                    print(f"[open_meteo] ensemble fetch failed model={model} ({lat},{lon}): "
                          f"{resp.status_code} Server Error — skipping retries")
                    api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
                    break
                resp.raise_for_status()
                data = resp.json()
                api_monitor.get_breaker("open_meteo").record_success()
                break
            except (ReqConnectionError, ReadTimeout, Timeout) as e:
                # Network-level failures indicate API is down — skip retries
                print(f"[open_meteo] ensemble fetch failed model={model} ({lat},{lon}): {e}")
                api_monitor.get_breaker("open_meteo").record_failure(retriable=True)
                break
            except Exception as e:
                if attempt < len(_RETRY_DELAYS):
                    continue   # retry on SSL errors, parse errors, etc.
                print(f"[open_meteo] ensemble fetch failed model={model} ({lat},{lon}): {e}")
                api_monitor.get_breaker("open_meteo").record_failure(retriable=True)

        if data is None:
            time.sleep(_INTER_REQUEST_DELAY)
            continue

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        member_keys = sorted(k for k in daily if "temperature_2m_max_member" in k)
        if member_keys and dates:
            for i, d in enumerate(dates):
                temps = [
                    float(daily[k][i])
                    for k in member_keys
                    if i < len(daily[k]) and daily[k][i] is not None
                ]
                combined.setdefault(d, []).extend(temps)

        time.sleep(_INTER_REQUEST_DELAY)  # pace requests to stay under rate limit

    if not combined:
        return []

    return [
        {"date": d, "member_temps": combined[d]}
        for d in sorted(combined)
    ]
