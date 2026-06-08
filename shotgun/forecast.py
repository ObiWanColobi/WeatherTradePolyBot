"""Live forecast -> 11-bucket density. Adapts Open-Meteo ensemble (°C) to the
research density math (°F)."""
from __future__ import annotations

from shotgun.bucket_boundaries import build_ladder
from shotgun.bucket_density import ensemble_to_density


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def members_f_for_date(ensemble: list[dict], target_date: str) -> list[float]:
    """From get_ensemble_forecasts() output, return that date's member daily-maxes in °F."""
    for entry in ensemble:
        if entry.get("date") == target_date:
            return [_c_to_f(float(t)) for t in entry.get("member_temps", []) if t is not None]
    return []


def build_density(members_f: list[float]):
    """Return (center_f, 11-bucket density) for the given member daily-maxes (°F).
    Returns (None, None) if no members."""
    if not members_f:
        return None, None
    center_f = sum(members_f) / len(members_f)
    ladder = build_ladder(center_f)
    density = ensemble_to_density(members_f, ladder)
    return center_f, density
