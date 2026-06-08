"""Bucket ladder construction + lookup helpers.

An 11-bucket ladder = 1 open-bottom tail + 9 closed 2°F-wide buckets + 1 open-top tail.
Centered on the integer-rounded forecast at T-48h per Q1 design decision (Option B).
"""
from typing import Sequence


def build_ladder(center_f: float) -> list[tuple[float | None, float | None]]:
    """Build an 11-bucket ladder centered on `center_f`.

    Bucket convention: a closed bucket (lo, hi) contains integer °F values v
    where lo <= v <= hi. The "64-65°F" Polymarket bucket = (64, 65) and contains
    both 64°F and 65°F observations.
    """
    c = int(round(center_f))
    ladder: list[tuple[float | None, float | None]] = []
    ladder.append((None, float(c - 9)))    # bottom tail "≤ c-9"
    lo = c - 8
    for _ in range(9):                      # 9 closed 2°F-wide buckets
        ladder.append((float(lo), float(lo + 1)))
        lo += 2
    ladder.append((float(c + 10), None))   # top tail "≥ c+10"
    assert len(ladder) == 11
    return ladder


def daily_max_to_bucket_idx(
    daily_max_f: float, ladder: Sequence[tuple[float | None, float | None]]
) -> int:
    f = int(round(daily_max_f))
    for i, (lo, hi) in enumerate(ladder):
        in_range = True
        if lo is not None and f < lo:
            in_range = False
        if hi is not None and f > hi:
            in_range = False
        if in_range:
            return i
    raise ValueError(f"daily_max {daily_max_f}°F → {f} didn't land in any bucket")
