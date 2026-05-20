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


from bucket_density import ensemble_to_density, deterministic_to_density


def test_ensemble_density_all_members_in_one_bucket():
    ladder = build_ladder(center_f=65)
    members_f = [64.4, 65.1, 64.8, 64.9, 65.0]
    # round(64.4)=64 -> bucket index 4 (63,64)
    # round(65.1)=65 -> bucket index 5 (65,66)
    # round(64.8)=65 -> bucket index 5
    # round(64.9)=65 -> bucket index 5
    # round(65.0)=65 -> bucket index 5
    d = ensemble_to_density(members_f, ladder)
    assert len(d) == 11
    assert d[4] == pytest.approx(1/5)
    assert d[5] == pytest.approx(4/5)
    assert sum(d) == pytest.approx(1.0)


def test_deterministic_density_single_bucket():
    ladder = build_ladder(center_f=65)
    d = deterministic_to_density(forecast_f=70.0, sigma_f=0.0, ladder=ladder)
    # Zero-sigma deterministic puts all mass in the bucket containing forecast
    # round(70)=70 -> bucket (69,70) = index 7
    assert d[7] == pytest.approx(1.0)
    assert sum(d) == pytest.approx(1.0)


def test_deterministic_density_with_sigma_spreads():
    ladder = build_ladder(center_f=65)
    d = deterministic_to_density(forecast_f=70.0, sigma_f=2.0, ladder=ladder)
    n_nonzero = sum(1 for x in d if x > 0.01)
    assert n_nonzero >= 3
    assert sum(d) == pytest.approx(1.0, abs=0.01)
