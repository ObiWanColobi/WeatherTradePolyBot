import importlib, tempfile, json
from shotgun.bets import compute_winset


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(1000.0)
    return db


def test_settle_fire_credits_winners(monkeypatch):
    db = _fresh(monkeypatch)
    fire_id = db.insert_fire(dict(
        fired_at_utc="t", city="toronto", resolution_date="2026-06-10", lead_hours=12.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=10.0,
        n_legs=2, status="open"))
    ws_win = compute_winset(69.0, 70.0, False)   # daily max 70 -> hits
    db.insert_bet(dict(fire_id=fire_id, market_id="mk1", sub_market_condition_id="m1",
        group_item_title="69-70F", side="yes", ladder_idx=5, density=0.3, mid_price=0.2,
        edge=0.1, stake_usd=5.0, fill_price=0.20, shares=25.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind=ws_win[0],
        winset_payload_json=json.dumps(ws_win[1]), price_trajectory_json="[]"))
    ws_lose = compute_winset(75.0, 76.0, False)
    db.insert_bet(dict(fire_id=fire_id, market_id="mk2", sub_market_condition_id="m2",
        group_item_title="75-76F", side="yes", ladder_idx=8, density=0.1, mid_price=0.1,
        edge=0.05, stake_usd=5.0, fill_price=0.10, shares=50.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind=ws_lose[0],
        winset_payload_json=json.dumps(ws_lose[1]), price_trajectory_json="[]"))

    bal_before = db.get_balance()
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    settled = shotgun_resolver.settle_fire(fire_id, daily_max_f=70.0)
    assert settled == 2
    bets = {b["sub_market_condition_id"]: b for b in db.get_all_bets_for_fire(fire_id)}
    assert bets["m1"]["status"] == "closed" and abs(bets["m1"]["pnl"] - (25.0 - 5.0)) < 1e-6  # win: 25 shares pay $25, cost 5
    assert bets["m2"]["status"] == "closed" and abs(bets["m2"]["pnl"] - (-5.0)) < 1e-6          # loss
    # balance credited by winner proceeds (25 shares * $1 = $25)
    assert abs(db.get_balance() - (bal_before + 25.0)) < 1e-6
    # fire marked closed
    assert db.get_open_fires() == []


def test_settle_fire_no_leg_pays_when_all_lose(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    ws = compute_winset(80.0, 81.0, False)
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="m", group_item_title="g",
        side="yes", ladder_idx=9, density=0.05, mid_price=0.05, edge=0.0, stake_usd=5.0,
        fill_price=0.05, shares=100.0, status="open", resolved_outcome=None, pnl=None,
        winset_kind=ws[0], winset_payload_json=json.dumps(ws[1]), price_trajectory_json="[]"))
    bal = db.get_balance()
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    shotgun_resolver.settle_fire(fid, daily_max_f=70.0)   # 70 not in 80-81 -> loss
    assert db.get_balance() == bal   # nothing credited
    assert db.get_all_bets_for_fire(fid)[0]["resolved_outcome"] == "loss"


def test_settle_fire_no_leg_pays_for_winning_no_side(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    ws = compute_winset(80.0, 81.0, False)   # bucket 80-81
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="m", group_item_title="g",
        side="no", ladder_idx=9, density=0.05, mid_price=0.95, edge=0.0, stake_usd=5.0,
        fill_price=0.95, shares=5.26, status="open", resolved_outcome=None, pnl=None,
        winset_kind=ws[0], winset_payload_json=json.dumps(ws[1]), price_trajectory_json="[]"))
    bal = db.get_balance()
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    shotgun_resolver.settle_fire(fid, daily_max_f=70.0)  # 70 NOT in 80-81 -> bucket NO -> NO bet WINS
    assert db.get_all_bets_for_fire(fid)[0]["resolved_outcome"] == "win"
    assert abs(db.get_balance() - (bal + 5.26)) < 1e-6   # 5.26 shares * $1


def test_settle_due_fires_uses_truth_fn(monkeypatch):
    import importlib, json as _json
    from shotgun.bets import compute_winset
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="toronto", resolution_date="2026-06-10",
        lead_hours=12.0, center_f=70.0, density_json="[]", budget_usd=50.0,
        total_staked_usd=5.0, n_legs=1, status="open"))
    ws = compute_winset(69.0, 70.0, False)
    db.insert_bet(dict(fire_id=fid, market_id="mk1", sub_market_condition_id="m1",
        group_item_title="69-70F", side="yes", ladder_idx=5, density=0.3, mid_price=0.2,
        edge=0.1, stake_usd=5.0, fill_price=0.2, shares=25.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind=ws[0],
        winset_payload_json=_json.dumps(ws[1]), price_trajectory_json="[]"))
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    truth = {("toronto", "2026-06-10"): 70.0}
    n = shotgun_resolver.settle_due_fires(truth_fn=lambda c, d: truth.get((c, d)))
    assert n == 1
    assert db.get_open_fires() == []


def test_settle_due_fires_skips_unknown_truth(monkeypatch):
    db = _fresh(monkeypatch)
    db.insert_fire(dict(fired_at_utc="t", city="toronto", resolution_date="2026-06-10",
        lead_hours=12.0, center_f=70.0, density_json="[]", budget_usd=50.0,
        total_staked_usd=5.0, n_legs=1, status="open"))
    import shotgun_resolver, importlib; importlib.reload(shotgun_resolver)
    n = shotgun_resolver.settle_due_fires(truth_fn=lambda c, d: None)  # truth unknown
    assert n == 0
    assert len(shotgun_resolver_open := __import__("db").get_open_fires()) == 1  # still open
