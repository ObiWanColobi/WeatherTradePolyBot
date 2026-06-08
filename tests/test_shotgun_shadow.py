"""Shadow (counterfactual) logging: classify_rejections, plan_fire's 4th return,
place_fire shadow writes, and shadow settlement (no balance effect)."""
import importlib
import json
import tempfile

from shotgun.bets import classify_rejections
from shotgun_strategy import plan_fire, ShotgunConfig


# ── classify_rejections (pure) ────────────────────────────────────────────────

def _row(idx, density, mid, **kw):
    base = dict(ladder_idx=idx, density=density, mid_price=mid,
                market_id=f"mk{idx}", sub_market_condition_id=f"c{idx}",
                group_item_title=f"{idx}C", winset_kind="closed",
                winset_payload_json="[70]")
    base.update(kw)
    return base


def test_classify_skips_selected_rows():
    # density vector strongly peaked at idx 5 -> core = {5}
    dv = [0.0]*5 + [1.0] + [0.0]*5
    rows = [_row(5, 0.50, 0.20)]   # in core, yes_edge 0.30, price ok -> SELECTED
    rejected = classify_rejections(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50)
    assert rejected == []   # selected rows are not shadowed


def test_classify_not_core():
    dv = [0.0]*5 + [1.0] + [0.0]*5   # core = {5}
    # idx 3: good yes edge + price band, but NOT in core
    rows = [_row(3, 0.40, 0.20)]
    rej = classify_rejections(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50)
    assert len(rej) == 1
    assert rej[0]["skip_reason"] == "not-core"
    assert rej[0]["would_side"] == "yes"
    assert rej[0]["in_core"] == 0
    assert abs(rej[0]["edge"] - 0.20) < 1e-9


def test_classify_yes_price_band_too_cheap():
    dv = [0.0]*5 + [1.0] + [0.0]*5   # core = {5}
    # idx 5 in core, big yes edge, but mid below price_min -> floored out
    rows = [_row(5, 0.40, 0.02)]
    rej = classify_rejections(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50)
    assert len(rej) == 1
    assert rej[0]["skip_reason"] == "yes-price-band"
    assert rej[0]["would_side"] == "yes"


def test_classify_no_cost_band():
    dv = [0.0]*5 + [1.0] + [0.0]*5
    # idx 3 not core; market overprices it (mid 0.62) vs density 0.10 -> NO edge
    # exists (mid-d=0.52) but NO cost 1-0.62=0.38 ok... make it fail the band:
    # mid 0.95 -> no_cost 0.05 ok; use mid 0.80 -> no_cost 0.20 ok. To FAIL band
    # we need no_cost outside [0.05,0.50] => mid<0.50. mid 0.40, d 0.05 ->
    # no_edge 0.35>=thresh, no_cost 0.60 > 0.50 -> band fail.
    rows = [_row(3, 0.05, 0.40)]
    rej = classify_rejections(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50)
    assert len(rej) == 1
    assert rej[0]["skip_reason"] == "no-cost-band"
    assert rej[0]["would_side"] == "no"


def test_classify_edge_below_threshold():
    dv = [0.0]*5 + [1.0] + [0.0]*5
    # idx 5 in core but tiny edge both sides
    rows = [_row(5, 0.22, 0.20)]   # yes_edge 0.02, no_edge -0.02
    rej = classify_rejections(rows, dv, "dist_yes_no", 0.50, 0.12, 0.05, 0.50)
    assert len(rej) == 1
    assert rej[0]["skip_reason"] == "edge<thresh"


# ── plan_fire returns rejected as 4th element ─────────────────────────────────

def _ens(lat, lon, tz):
    return [{"date": "2026-06-10", "member_temps": [21.0]*10}]


def _buckets():
    return [
        dict(sub_market_condition_id="m1", group_item_title="69-70°F",
             bound_lo_f=69.0, bound_hi_f=70.0, is_open_tail=0, mid_price=0.20,
             best_bid=0.19, best_ask=0.21, liquidity_num=1000, volume_24h=500,
             token_id="t1", no_token_id="n1", market_id="mk1"),
        # a far, cheap bucket that should be rejected (not-core / price band)
        dict(sub_market_condition_id="m2", group_item_title="80-81°F",
             bound_lo_f=80.0, bound_hi_f=81.0, is_open_tail=0, mid_price=0.02,
             best_bid=0.01, best_ask=0.03, liquidity_num=1000, volume_24h=500,
             token_id="t2", no_token_id="n2", market_id="mk2"),
    ]


def test_plan_fire_returns_rejected_list():
    cfg = ShotgunConfig(edge_threshold=0.05, mass_core_frac=0.5,
                        budget_per_city_day=50.0, per_bucket_liq_cap_frac=0.0)
    bets, center_f, density, rejected = plan_fire(
        "toronto", "2026-06-10", _buckets(), cfg,
        {"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"}, _ens)
    assert center_f is not None
    assert isinstance(rejected, list)
    # rejected rows carry the contract fields the shadow table needs
    for r in rejected:
        assert r["skip_reason"]
        assert r["would_side"] in ("yes", "no")
        assert "sub_market_condition_id" in r


# ── place_fire writes shadow rows; settlement records hypo_pnl, no balance ────

def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(2000.0)
    return db


def test_place_fire_persists_shadow_and_does_not_debit(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    monkeypatch.setattr(ex, "_simulate_fill", lambda t, s, sz: (0.21, sz))

    real_bet = dict(side="yes", token_id="t1", no_token_id="n1", stake_usd=50.0,
                    market_id="mk1", sub_market_condition_id="c1",
                    group_item_title="70C", ladder_idx=5, density=0.4, mid_price=0.20,
                    edge=0.20, winset_kind="closed", winset_payload_json="[70]")
    shadow = [dict(market_id="mk2", sub_market_condition_id="c2", group_item_title="80C",
                   would_side="yes", skip_reason="yes-price-band", ladder_idx=9,
                   density=0.3, mid_price=0.02, edge=0.28, in_core=0,
                   winset_kind="closed", winset_payload_json="[80]")]

    bal_before = db.get_balance()
    fid = ex.place_fire(city="toronto", resolution_date="2026-06-10", lead_hours=12.0,
                        center_f=70.0, density_json="[]", budget_usd=50.0,
                        bets=[real_bet], shadow_candidates=shadow)
    assert fid > 0
    # real leg debited; shadow did NOT add to the debit
    assert abs(db.get_balance() - (bal_before - 50.0)) < 1e-6
    shadows = db.get_shadow_bets_for_fire(fid)
    assert len(shadows) == 1
    assert shadows[0]["skip_reason"] == "yes-price-band"
    assert shadows[0]["status"] == "open"


def test_shadow_settles_without_touching_balance(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=0.0,
        n_legs=0, status="open"))
    # one real leg (so the fire can close) + one shadow leg
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="real",
        group_item_title="g", side="yes", ladder_idx=5, density=0.3, mid_price=0.2,
        edge=0.1, stake_usd=5.0, fill_price=0.2, shares=25.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind="closed",
        winset_payload_json="[70]", price_trajectory_json="[]"))
    db.insert_shadow_bet(dict(fire_id=fid, market_id="mk2", sub_market_condition_id="shadow",
        group_item_title="g2", would_side="yes", skip_reason="yes-price-band",
        ladder_idx=9, density=0.3, mid_price=0.20, edge=0.10, in_core=0, status="open",
        resolved_outcome=None, hypo_pnl=None, winset_kind="closed", winset_payload_json="[80]"))

    import shotgun_resolver
    bal_before = db.get_balance()
    # both markets resolve YES
    shotgun_resolver.settle_due_fires_polymarket(
        resolution_fetch=lambda cid: {"resolved": True, "yes_price": 1.0})

    shadows = db.get_shadow_bets_for_fire(fid)
    assert shadows[0]["status"] == "closed"
    assert shadows[0]["resolved_outcome"] == "win"
    # $1 notional on a 0.20 YES that wins -> shares 5, pnl = 5-1 = 4.0
    assert abs(shadows[0]["hypo_pnl"] - 4.0) < 1e-6
    # shadow settlement credited NOTHING beyond the real leg's payout (25 shares*$1)
    assert abs(db.get_balance() - (bal_before + 25.0)) < 1e-6
    # fire closed (real leg done; shadow doesn't gate)
    assert db.get_open_fires() == []
