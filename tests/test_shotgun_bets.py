from shotgun.bets import mass_core_indices, build_bucket_bets, compute_winset, winset_resolved


def test_mass_core_picks_smallest_set_to_frac():
    d = [0.0, 0.1, 0.4, 0.3, 0.2] + [0.0]*6
    assert mass_core_indices(d, 0.5) == {2, 3}


def test_mass_core_full_returns_all_nonzero():
    d = [0.0, 0.5, 0.5] + [0.0]*8
    assert mass_core_indices(d, 1.0) == {1, 2}


def test_build_bucket_bets_dist_yes_no_emits_yes_and_no_legs():
    dv = [0.0]*11; dv[5] = 0.6; dv[6] = 0.4
    rows = [
        dict(ladder_idx=5, density=0.6, mid_price=0.30, snapshot_id=1, group_item_title="A",
             winset_kind="closed", winset_payload_json="[70]", center_f=70.0, bucket_idx=5,
             edge=0.30, volume_24h=500, best_bid=0.29, best_ask=0.31, liquidity_num=1000,
             snapshot_at_utc="t", city="x", resolution_date="d", source="s", event_slug="e"),
        dict(ladder_idx=6, density=0.10, mid_price=0.40, snapshot_id=2, group_item_title="B",
             winset_kind="closed", winset_payload_json="[72]", center_f=70.0, bucket_idx=6,
             edge=-0.30, volume_24h=500, best_bid=0.39, best_ask=0.41, liquidity_num=1000,
             snapshot_at_utc="t", city="x", resolution_date="d", source="s", event_slug="e"),
    ]
    bets = build_bucket_bets(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50, "weighted", 50.0)
    sides = sorted(b["side"] for b in bets)
    assert "yes" in sides
    assert abs(sum(b["stake_usd"] for b in bets) - 50.0) < 1e-6


def test_winset_resolved_closed_range():
    ws = compute_winset(64.0, 65.0, False)
    assert winset_resolved(ws, 64.4) is True
    assert winset_resolved(ws, 66.0) is False
