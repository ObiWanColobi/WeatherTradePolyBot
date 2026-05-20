"""Unit tests for bucket boundary + density + RPS math.
Pure functions — no DB, no API."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "research_db"))

import pytest

from bucket_boundaries import build_ladder, daily_max_to_bucket_idx


def test_build_ladder_centered_at_65_F():
    """Center at 65°F: 9 closed buckets ±9°F + 2 open tails = 11 total."""
    ladder = build_ladder(center_f=65)
    assert len(ladder) == 11
    # bottom tail
    assert ladder[0] == (None, 56)        # "56°F or below"
    # closed buckets, 2°F wide
    assert ladder[1] == (57, 58)
    assert ladder[2] == (59, 60)
    # center bucket contains 65
    assert ladder[5] == (65, 66)
    # top tail
    assert ladder[10] == (75, None)


def test_build_ladder_handles_negative_center():
    ladder = build_ladder(center_f=-5)
    # First-pass implementation should still produce 11 buckets symmetric around -5
    assert len(ladder) == 11
    # center bucket should contain -5
    centers = [(lo + hi) / 2 if lo is not None and hi is not None else None for lo, hi in ladder]
    assert any(c is not None and abs(c - (-5)) <= 1 for c in centers)


def test_daily_max_to_bucket_idx_inside_closed_bucket():
    ladder = build_ladder(center_f=65)
    # 64°F integer-rounded -> should land in (63,64) bucket
    assert daily_max_to_bucket_idx(daily_max_f=64, ladder=ladder) == 4   # (63,64) is index 4


def test_daily_max_to_bucket_idx_open_top_tail():
    ladder = build_ladder(center_f=65)
    # 95°F is above all closed buckets -> top tail index 10
    assert daily_max_to_bucket_idx(daily_max_f=95, ladder=ladder) == 10


def test_daily_max_to_bucket_idx_open_bottom_tail():
    ladder = build_ladder(center_f=65)
    # 40°F is below all closed buckets -> bottom tail index 0
    assert daily_max_to_bucket_idx(daily_max_f=40, ladder=ladder) == 0
