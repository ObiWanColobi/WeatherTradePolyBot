"""Unit tests for distribution-shotgun helpers. Pure functions, no DB."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "research_db"))

import pytest
from snapshot_pnl import mass_core_indices, build_bucket_bets


def test_mass_core_sharp_forecast_is_narrow():
    # One bucket holds 80% — exceeds 0.70 alone.
    d = [0.0, 0.0, 0.8, 0.1, 0.05, 0.05, 0, 0, 0, 0, 0]
    assert mass_core_indices(d, 0.70) == {2}


def test_mass_core_fuzzy_forecast_is_wide():
    # Flat-ish: need several buckets to reach 0.70.
    d = [0.05, 0.10, 0.20, 0.25, 0.20, 0.10, 0.05, 0.05, 0, 0, 0]
    # Sorted desc: 0.25(3),0.20(2),0.20(4) -> cum 0.65; +0.10(1) -> 0.75 >=0.70
    assert mass_core_indices(d, 0.70) == {1, 2, 3, 4}


def test_mass_core_ties_broken_by_index():
    # Two buckets at 0.40 each; 0.70 reached after both.
    d = [0.40, 0.40, 0.20, 0, 0, 0, 0, 0, 0, 0, 0]
    assert mass_core_indices(d, 0.70) == {0, 1}


def test_mass_core_frac_one_takes_all_nonzero():
    d = [0.5, 0.3, 0.2, 0, 0, 0, 0, 0, 0, 0, 0]
    assert mass_core_indices(d, 1.0) == {0, 1, 2}


def _row(ladder_idx, density, mid_price, **extra):
    r = dict(ladder_idx=ladder_idx, density=density, mid_price=mid_price,
             snapshot_id=1, city="x", resolution_date="2026-05-22",
             group_item_title=f"b{ladder_idx}", winset_kind="closed",
             winset_payload_json="[70]", volume_24h=500.0, source="om_icon_hist",
             event_slug="e", center_f=70.0, bucket_idx=ladder_idx, edge=density-mid_price)
    r.update(extra)
    return r


# density vector: peak at idx 3
DV = [0.02, 0.05, 0.15, 0.40, 0.20, 0.10, 0.05, 0.02, 0.01, 0, 0]


def test_edge_shotgun_takes_all_positive_edge_in_band():
    rows = [_row(2, 0.15, 0.05), _row(3, 0.40, 0.30), _row(7, 0.02, 0.50)]
    bets = build_bucket_bets(rows, DV, mode="edge_shotgun", mass_core_frac=0.70,
                             edge_threshold=0.05, price_min=0.05, price_max=0.50,
                             sizing="flat")
    # idx2 edge 0.10>=.05 in-band; idx3 edge 0.10>=.05 in-band; idx7 edge -0.48 fails
    assert {b["ladder_idx"] for b in bets} == {2, 3}
    assert all(b["side"] == "yes" and b["stake_usd"] == 1.0 for b in bets)


def test_dist_yes_weighted_splits_one_dollar_by_density():
    # mass-core for DV @0.70: 0.40(3),0.20(4),0.15(2) -> cum .75 => {2,3,4}
    rows = [_row(2, 0.15, 0.05), _row(3, 0.40, 0.30), _row(4, 0.20, 0.10)]
    bets = build_bucket_bets(rows, DV, mode="dist_yes", mass_core_frac=0.70,
                             edge_threshold=0.05, price_min=0.05, price_max=0.50,
                             sizing="weighted")
    # all three in core AND underpriced (edges .10,.10,.10) -> all YES
    assert {b["ladder_idx"] for b in bets} == {2, 3, 4}
    assert sum(b["stake_usd"] for b in bets) == pytest.approx(1.0)
    # idx3 has highest density so biggest stake
    s = {b["ladder_idx"]: b["stake_usd"] for b in bets}
    assert s[3] > s[4] > s[2]


def test_dist_yes_no_mirror_no_avoids_tail_trap():
    # A cheap tail bucket idx0: mid 0.04 -> NO cost 0.96 (outside band) -> NO trade.
    # idx3 in core + underpriced -> YES.
    # idx5: density 0.10, mid 0.30 -> we think OVERpriced: mid-density=0.20>=.05;
    #   NO cost = 0.70 in [0.05,0.95] -> NO bet.
    rows = [_row(0, 0.02, 0.04), _row(3, 0.40, 0.30), _row(5, 0.10, 0.30)]
    bets = build_bucket_bets(rows, DV, mode="dist_yes_no", mass_core_frac=0.70,
                             edge_threshold=0.05, price_min=0.05, price_max=0.95,
                             sizing="weighted")
    sides = {b["ladder_idx"]: b["side"] for b in bets}
    assert sides.get(3) == "yes"
    assert 0 not in sides            # idx0 NO-cost 0.96 > 0.95 max -> excluded
    assert sides.get(5) == "no"
    assert sum(b["stake_usd"] for b in bets) == pytest.approx(1.0)


def test_yes_and_no_sets_never_overlap():
    rows = [_row(i, DV[i], 0.10) for i in range(8)]
    bets = build_bucket_bets(rows, DV, mode="dist_yes_no", mass_core_frac=0.70,
                             edge_threshold=0.05, price_min=0.05, price_max=0.95,
                             sizing="flat")
    yes_idx = {b["ladder_idx"] for b in bets if b["side"] == "yes"}
    no_idx = {b["ladder_idx"] for b in bets if b["side"] == "no"}
    assert yes_idx.isdisjoint(no_idx)
