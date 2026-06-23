"""Common candidate contract. Every candidate module implements evaluate()."""
from __future__ import annotations
from typing import Protocol
from bakeoff.harness.loader import CityDay


class Candidate(Protocol):
    NAME: str
    FORECAST_BASED: bool
    def evaluate(self, city_day: CityDay, fee_rate: float, params: dict | None) -> list[dict]:
        ...
