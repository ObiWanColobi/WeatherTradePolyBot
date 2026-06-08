import importlib, tempfile


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(1000.0)
    return db


def _leg(db, fire_id, cond, side, shares, stake):
    return db.insert_bet(dict(fire_id=fire_id, market_id="mk", sub_market_condition_id=cond,
        group_item_title="g", side=side, ladder_idx=5, density=0.3, mid_price=0.2, edge=0.1,
        stake_usd=stake, fill_price=0.2, shares=shares, status="open", resolved_outcome=None,
        pnl=None, winset_kind="closed", winset_payload_json="[70]", price_trajectory_json="[]"))


def test_yes_leg_wins_when_bucket_resolves_yes(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    _leg(db, fid, "cond_win", "yes", shares=25.0, stake=5.0)
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    fetch = lambda cid: {"resolved": True, "yes_price": 1.0}
    bal = db.get_balance()
    n = shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=fetch)
    assert n == 1
    b = db.get_all_bets_for_fire(fid)[0]
    assert b["resolved_outcome"] == "win" and abs(b["pnl"] - 20.0) < 1e-6
    assert abs(db.get_balance() - (bal + 25.0)) < 1e-6
    assert db.get_open_fires() == []   # all legs closed -> fire closed


def test_no_leg_wins_when_bucket_resolves_no(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    _leg(db, fid, "cond_no", "no", shares=10.0, stake=5.0)
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    fetch = lambda cid: {"resolved": True, "yes_price": 0.0}   # bucket MISS -> NO leg wins
    bal = db.get_balance()
    shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=fetch)
    b = db.get_all_bets_for_fire(fid)[0]
    assert b["resolved_outcome"] == "win" and abs(db.get_balance() - (bal + 10.0)) < 1e-6


def test_unresolved_leg_left_open_and_fire_stays_open(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=10.0, n_legs=2, status="open"))
    _leg(db, fid, "cond_done", "yes", shares=25.0, stake=5.0)
    _leg(db, fid, "cond_pending", "yes", shares=25.0, stake=5.0)
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    # first leg resolved YES, second not resolved yet
    def fetch(cid):
        return {"resolved": True, "yes_price": 1.0} if cid == "cond_done" else {"resolved": False}
    n = shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=fetch)
    assert n == 1
    statuses = sorted(b["status"] for b in db.get_all_bets_for_fire(fid))
    assert statuses == ["closed", "open"]
    assert len(db.get_open_fires()) == 1   # one leg still open -> fire stays open


def test_fetch_failure_leaves_leg_open(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    _leg(db, fid, "cond_fail", "yes", shares=25.0, stake=5.0)
    import shotgun_resolver; importlib.reload(shotgun_resolver)
    n = shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=lambda cid: None)  # API failed
    assert n == 0
    assert db.get_all_bets_for_fire(fid)[0]["status"] == "open"
    assert len(db.get_open_fires()) == 1
