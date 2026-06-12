"""Tests for the 2026-06-11 review fixes (H1/H2/H3/M1/M2 + density floor).

Each test pins the corrected behavior so a revert is caught. See
tasks/findings/2026-06-11_full_code_and_strategy_review.md.
"""
import importlib
import tempfile

from shotgun.bets import (
    winset_density, compute_winset, build_bucket_bets, classify_rejections, _exec_prices,
)
from shotgun.bucket_boundaries import build_ladder


# ── H2: tail density honors the market's payload threshold, not the ladder edge ──

def test_tail_top_density_uses_payload_threshold_not_ladder_edge():
    # center 70 -> ladder top tail is "≥ 80°F" (c+10). A market tail "≥ 74°F" must
    # collect density from the 74-79 closed buckets too, not just the ≥80 tail bucket.
    ladder = build_ladder(70.0)
    density = [0.0] * 11
    density[-1] = 0.05          # ladder top tail (≥80)
    density[7] = 0.10           # bucket (74,75)
    density[8] = 0.10           # bucket (76,77)
    density[9] = 0.10           # bucket (78,79)
    ws = compute_winset(74.0, None, True)   # market tail "≥ 74"
    assert ws[0] == "tail_top" and ws[1] == 74
    d = winset_density(ws, ladder, density)
    # idx7=(74,75) both ≥74 -> full 0.10; idx8=(76,77) full; idx9=(78,79) full; ≥80 tail full
    assert d > 0.05            # strictly more than the bare ladder-tail mass (the old bug)
    assert abs(d - (0.10 + 0.10 + 0.10 + 0.05)) < 1e-9


def test_tail_top_density_threshold_above_all_closed_buckets():
    ladder = build_ladder(70.0)
    density = [0.0] * 11
    density[-1] = 0.07
    ws = compute_winset(85.0, None, True)   # ≥85, inside the unbounded ≥80 ladder tail
    d = winset_density(ws, ladder, density)
    # the open top tail is unbounded above, so it's our entire estimate of mass ≥85
    assert abs(d - 0.07) < 1e-9


def test_tail_bottom_density_uses_payload_threshold():
    ladder = build_ladder(70.0)              # bottom tail "≤ 61°F" (c-9)
    density = [0.0] * 11
    density[0] = 0.04                         # ≤61 ladder tail
    density[1] = 0.10                         # bucket (62,63)
    ws = compute_winset(None, 63.0, True)     # market tail "≤ 63"
    assert ws[0] == "tail_bottom" and ws[1] == 63
    d = winset_density(ws, ladder, density)
    assert abs(d - (0.04 + 0.10)) < 1e-9      # full ≤61 tail + full (62,63) bucket


# ── H3: edge scored at the executable touch (best_ask / 1-best_bid), not mid ──────

def test_exec_prices_use_touch_when_book_present():
    r = dict(mid_price=0.20, best_bid=0.18, best_ask=0.25)
    yes_price, no_cost = _exec_prices(r)
    assert yes_price == 0.25                  # YES pays the ask
    assert abs(no_cost - (1.0 - 0.18)) < 1e-9 # NO pays 1 - bid


def test_exec_prices_fall_back_to_mid_without_book():
    r = dict(mid_price=0.30)
    yes_price, no_cost = _exec_prices(r)
    assert yes_price == 0.30
    assert abs(no_cost - 0.70) < 1e-9


def test_yes_edge_at_ask_can_flip_selection_below_threshold():
    # density 0.30, mid 0.20 -> mid-edge 0.10 (passes 0.08). At ask 0.25 -> edge 0.05 (fails).
    dv = [0.0] * 11; dv[5] = 1.0
    rows = [dict(ladder_idx=5, density=0.30, mid_price=0.20, best_bid=0.15, best_ask=0.25,
                 group_item_title="A", winset_kind="closed", winset_payload_json="[70]")]
    bets = build_bucket_bets(rows, dv, "dist_yes_no", 1.0, 0.08, 0.05, 0.50, "weighted", 50.0)
    yes = [b for b in bets if b["side"] == "yes"]
    assert not yes                            # ask-scored edge 0.05 < 0.08 -> not selected


# ── M1: shadow rows carry book state so the lever analysis prices at the touch ────

def test_shadow_rows_persist_book_state():
    dv = [0.0] * 11; dv[5] = 1.0
    # bucket fails the YES price band (ask above price_max) but had core+edge -> shadow
    rows = [dict(ladder_idx=5, density=0.90, mid_price=0.60, best_bid=0.58, best_ask=0.62,
                 liquidity_num=777, group_item_title="A",
                 winset_kind="closed", winset_payload_json="[70]")]
    rej = classify_rejections(rows, dv, "dist_yes_no", 1.0, 0.10, 0.05, 0.50)
    assert rej, "expected a shadow row"
    s = rej[0]
    assert s["best_bid"] == 0.58 and s["best_ask"] == 0.62 and s["liquidity_num"] == 777


# ── density floor + place_fire fee/gas + H3 cap (integration via place_fire) ───────

def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(2000.0)
    return db


def test_place_fire_charges_fee_and_gas(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.20, size))
    bets = [dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
                 sub_market_condition_id="m1", group_item_title="g", ladder_idx=5, density=0.4,
                 mid_price=0.20, best_bid=0.19, best_ask=0.21, edge=0.20, stake_usd=10.0,
                 liquidity_num=1000, winset_kind="closed", winset_payload_json="[70]")]
    fire_id = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                            density_json="[]", budget_usd=50.0, bets=bets)
    leg = db.get_all_bets_for_fire(fire_id)[0]
    # frictionless shares would be 10/0.20 = 50; fee+gas must reduce them
    assert leg["shares"] < 50.0
    assert leg["fee_usd"] is not None and leg["fee_usd"] > 0
    # book state persisted (price_trajectory_json no longer "[]")
    import json
    traj = json.loads(leg["price_trajectory_json"])
    assert traj and traj[0]["best_ask"] == 0.21


def test_place_fire_per_leg_fill_cap_drops_through_book_legs(monkeypatch):
    db = _fresh(monkeypatch)
    import config
    config.SHOTGUN["max_fill_slippage_per_leg"] = 0.10
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    # scored price for YES = best_ask 0.20; fill walks book to 0.40 (slip 0.20 > 0.10)
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.40, size))
    bets = [dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
                 sub_market_condition_id="m1", group_item_title="g", ladder_idx=5, density=0.6,
                 mid_price=0.20, best_bid=0.19, best_ask=0.20, edge=0.40, stake_usd=10.0,
                 liquidity_num=1000, winset_kind="closed", winset_payload_json="[70]")]
    fire_id = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                            density_json="[]", budget_usd=50.0, bets=bets)
    assert fire_id == 0                       # the only leg was capped out -> no fire


def test_place_fire_skips_legs_over_balance(monkeypatch):
    db = _fresh(monkeypatch)
    db.set_balance(5.0)                        # less than the 10 stake
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.20, size))
    bets = [dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
                 sub_market_condition_id="m1", group_item_title="g", ladder_idx=5, density=0.4,
                 mid_price=0.20, best_bid=0.19, best_ask=0.21, edge=0.2, stake_usd=10.0,
                 liquidity_num=1000, winset_kind="closed", winset_payload_json="[70]")]
    fire_id = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                            density_json="[]", budget_usd=50.0, bets=bets)
    assert fire_id == 0
    assert db.get_balance() == 5.0            # never went negative


# ── Toronto/Americas fire-window: discovery filter anchors on city-local close ────

def test_americas_event_survives_discovery_filter_when_in_window():
    # Toronto june-10 event: Polymarket stamps endDate at noon UTC, but the city-local
    # close is june-11 03:59 UTC (window opens june-10 15:59 UTC). At 16:00 UTC june-10
    # the event MUST NOT be filtered out (the old endDate<now check dropped it at noon).
    from datetime import datetime, timezone
    from markets.polymarket import _event_is_past_close
    event = {"slug": "highest-temperature-in-toronto-on-june-10-2026",
             "endDate": "2026-06-10T12:00:00Z"}
    now = datetime(2026, 6, 10, 16, 0, 0, tzinfo=timezone.utc)   # in toronto's window
    assert _event_is_past_close(event, now, now.isoformat()) is False


def test_americas_event_filtered_after_local_close_plus_grace():
    from datetime import datetime, timezone
    from markets.polymarket import _event_is_past_close
    event = {"slug": "highest-temperature-in-toronto-on-june-10-2026",
             "endDate": "2026-06-10T12:00:00Z"}
    # close = june-11 03:59 UTC; +6h grace = june-11 09:59 UTC. After that -> filtered.
    now = datetime(2026, 6, 11, 11, 0, 0, tzinfo=timezone.utc)
    assert _event_is_past_close(event, now, now.isoformat()) is True


def test_unparseable_slug_falls_back_to_enddate():
    from datetime import datetime, timezone
    from markets.polymarket import _event_is_past_close
    event = {"slug": "not-a-weather-slug", "endDate": "2026-06-10T12:00:00Z"}
    now = datetime(2026, 6, 10, 13, 0, 0, tzinfo=timezone.utc)
    assert _event_is_past_close(event, now, now.isoformat()) is True   # endDate < now


# ── M2: orphaned shadow bets settle even after their fire closed ──────────────────

def test_orphan_shadow_settles_after_fire_closed(monkeypatch):
    db = _fresh(monkeypatch)
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.20, size))
    bets = [dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
                 sub_market_condition_id="cond_real", group_item_title="g", ladder_idx=5,
                 density=0.4, mid_price=0.20, best_bid=0.19, best_ask=0.21, edge=0.2,
                 stake_usd=10.0, liquidity_num=1000,
                 winset_kind="closed", winset_payload_json="[70]")]
    shadows = [dict(sub_market_condition_id="cond_shadow", would_side="yes", skip_reason="not-core",
                    ladder_idx=6, density=0.1, mid_price=0.30, best_bid=0.29, best_ask=0.31,
                    liquidity_num=500, edge=0.0, in_core=0, group_item_title="s",
                    winset_kind="closed", winset_payload_json="[72]", market_id="mk2")]
    fire_id = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                            density_json="[]", budget_usd=50.0, bets=bets,
                            shadow_candidates=shadows)
    # First pass: real leg resolves, fire closes, but shadow is NOT yet resolved.
    def res1(cond):
        if cond == "cond_real":
            return {"resolved": True, "yes_price": 1.0}
        return {"resolved": False}              # shadow not resolved yet
    shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=res1)
    assert db.get_open_fires() == []            # fire closed on its real leg
    assert len(db.get_open_shadow_bets()) == 1  # shadow orphaned open

    # Second pass: shadow's market resolves. The orphan sweep must still catch it.
    def res2(cond):
        return {"resolved": True, "yes_price": 1.0}
    shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=res2)
    assert db.get_open_shadow_bets() == []      # orphan settled despite closed fire
