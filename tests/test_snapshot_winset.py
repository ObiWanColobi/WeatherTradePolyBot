"""Unit tests for snapshot win-set helpers (bucket-type-aware matching).

Covers the three Polymarket bucket formats:
  range   ("65-66°F"): bound_lo_f != bound_hi_f, integer °F range, both inclusive
  exact   ("21°C"):    bound_lo_f == bound_hi_f, °F-equivalent of an integer °C value
  tail    ("≤56°F"):   one bound is NULL, is_open_tail=1
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "research_db"))

import pytest

from snapshot_replay import (
    compute_winset,
    integer_fs_for_exact_c,
    winset_density,
    winset_resolved,
    primary_bucket_idx,
)
from bucket_boundaries import build_ladder


# ── compute_winset ──────────────────────────────────────────────────────────────

def test_winset_range_simple():
    # "65-66°F" range: integer °F values 65 and 66 win
    ws = compute_winset(65.0, 66.0, is_open_tail=False)
    assert ws == ("closed", [65, 66])


def test_winset_range_inclusive_endpoints():
    # A 3-integer range "63-65°F"
    ws = compute_winset(63.0, 65.0, is_open_tail=False)
    assert ws == ("closed", [63, 64, 65])


def test_winset_exact_c_21():
    # "21°C" = 69.8°F. Win-set = integer °F values whose round-to-int-°C == 21.
    # round((69-32)*5/9) = round(20.56) = 21 ✓
    # round((70-32)*5/9) = round(21.11) = 21 ✓
    # round((68-32)*5/9) = round(20.00) = 20 ✗
    # round((71-32)*5/9) = round(21.67) = 22 ✗
    ws = compute_winset(69.8, 69.8, is_open_tail=False)
    assert ws == ("closed", [69, 70])


def test_winset_exact_c_18():
    # "18°C" = 64.4°F. round((64-32)*5/9) = round(17.78) = 18 ✓; round((65-...)) = round(18.33) = 18 ✓.
    # round((63-...)) = round(17.22) = 17; round((66-...)) = round(18.89) = 19.
    ws = compute_winset(64.4, 64.4, is_open_tail=False)
    assert ws == ("closed", [64, 65])


def test_winset_tail_bottom():
    # "≤56°F" — open-bottom tail
    ws = compute_winset(None, 56.0, is_open_tail=True)
    assert ws == ("tail_bottom", 56)


def test_winset_tail_top():
    # "≥75°F" — open-top tail
    ws = compute_winset(75.0, None, is_open_tail=True)
    assert ws == ("tail_top", 75)


def test_winset_malformed_returns_none():
    assert compute_winset(None, None, is_open_tail=False) is None
    # is_open_tail=1 but both bounds non-NULL is malformed
    assert compute_winset(60.0, 70.0, is_open_tail=True) is None


# ── winset_resolved ─────────────────────────────────────────────────────────────

def test_resolved_closed_hit():
    ws = ("closed", [65, 66])
    assert winset_resolved(ws, 65.4) is True   # rounds to 65
    assert winset_resolved(ws, 65.5) is True   # rounds to 66 (banker's round even; Python rounds 0.5→even)
    assert winset_resolved(ws, 66.0) is True


def test_resolved_closed_miss():
    ws = ("closed", [65, 66])
    assert winset_resolved(ws, 64.3) is False
    assert winset_resolved(ws, 67.1) is False


def test_resolved_tail_bottom():
    ws = ("tail_bottom", 56)
    assert winset_resolved(ws, 55.4) is True   # rounds to 55, <= 56
    assert winset_resolved(ws, 56.4) is True   # rounds to 56, <= 56
    assert winset_resolved(ws, 56.6) is False  # rounds to 57, > 56


def test_resolved_tail_top():
    ws = ("tail_top", 75)
    assert winset_resolved(ws, 75.0) is True
    assert winset_resolved(ws, 80.4) is True
    assert winset_resolved(ws, 74.4) is False  # rounds to 74, < 75


# ── winset_density ──────────────────────────────────────────────────────────────

def test_density_range_full_bucket():
    """Range "65-66°F" centered at 65 should = full ladder bucket density."""
    ladder = build_ladder(65)
    # ladder[5] = (65, 66). Synthesize a density that puts all mass at bucket 5.
    density = [0.0] * 11
    density[5] = 0.7
    density[6] = 0.3
    ws = ("closed", [65, 66])  # range "65-66°F"
    # Both 65 and 66 fall in ladder[5]; width=2 → contribution = 0.7 * 2/2 = 0.7
    assert winset_density(ws, ladder, density) == pytest.approx(0.7)


def test_density_exact_within_one_bucket():
    """21°C = {69, 70} fall in one ladder bucket at center 77°F → ladder[1] = (69, 70)."""
    ladder = build_ladder(77)
    assert ladder[1] == (69, 70)
    density = [0.0] * 11
    density[1] = 0.4
    density[2] = 0.6
    ws = ("closed", [69, 70])
    # Both 69 and 70 in ladder[1]; width=2 → contribution = 0.4 * 2/2 = 0.4
    assert winset_density(ws, ladder, density) == pytest.approx(0.4)


def test_density_exact_spans_two_buckets():
    """18°C = {64, 65} at center_f=65 lands across ladder bucket 4 (63,64) and 5 (65,66)."""
    ladder = build_ladder(65)
    assert ladder[4] == (63, 64)
    assert ladder[5] == (65, 66)
    density = [0.0] * 11
    density[4] = 0.20  # contains 63, 64
    density[5] = 0.30  # contains 65, 66
    ws = ("closed", [64, 65])
    # 64 in ladder[4]: 0.20 * 1/2 = 0.10
    # 65 in ladder[5]: 0.30 * 1/2 = 0.15
    assert winset_density(ws, ladder, density) == pytest.approx(0.25)


def test_density_tail_bottom_returns_full_bucket():
    ladder = build_ladder(65)
    density = [0.0] * 11
    density[0] = 0.05  # bottom-tail mass
    ws = ("tail_bottom", 56)
    assert winset_density(ws, ladder, density) == pytest.approx(0.05)


def test_density_tail_top_returns_full_bucket():
    ladder = build_ladder(65)
    density = [0.0] * 11
    density[10] = 0.08
    ws = ("tail_top", 75)
    assert winset_density(ws, ladder, density) == pytest.approx(0.08)


def test_density_winset_outside_ladder_is_zero():
    """A range whose integers fall completely outside the closed ladder returns 0."""
    ladder = build_ladder(65)
    density = [0.1] * 11
    ws = ("closed", [200, 201])  # way too hot
    assert winset_density(ws, ladder, density) == pytest.approx(0.0)


# ── primary_bucket_idx ──────────────────────────────────────────────────────────

def test_primary_bucket_idx_range():
    ladder = build_ladder(65)
    ws = ("closed", [65, 66])
    assert primary_bucket_idx(ws, ladder) == 5  # ladder[5] = (65, 66)


def test_primary_bucket_idx_tails():
    ladder = build_ladder(65)
    assert primary_bucket_idx(("tail_bottom", 56), ladder) == 0
    assert primary_bucket_idx(("tail_top", 75), ladder) == 10


# ── integer_fs_for_exact_c ──────────────────────────────────────────────────────

def test_integer_fs_for_exact_c_21():
    assert integer_fs_for_exact_c(69.8) == [69, 70]


def test_integer_fs_for_exact_c_freezing():
    # 0°C = 32°F. round((31-32)*5/9) = round(-0.56) = -1 (no, Python banker's round 0.5 to even).
    # round((32-32)*5/9) = round(0) = 0 ✓
    # round((33-32)*5/9) = round(0.56) = 1
    # So only 32 matches.
    assert integer_fs_for_exact_c(32.0) == [32]
