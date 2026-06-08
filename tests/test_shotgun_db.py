import tempfile, importlib, os


def _fresh_db(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db)
    db.init_db()
    return db


def test_insert_fire_and_bets_roundtrip(monkeypatch):
    db = _fresh_db(monkeypatch)
    fire_id = db.insert_fire(dict(
        fired_at_utc="2026-06-10T12:00:00Z", city="toronto", resolution_date="2026-06-10",
        lead_hours=12.0, center_f=70.0, density_json="[]", budget_usd=50.0,
        total_staked_usd=48.0, n_legs=2, status="open"))
    assert fire_id > 0
    db.insert_bet(dict(
        fire_id=fire_id, market_id="mk1", sub_market_condition_id="m1",
        group_item_title="69-70F", side="yes", ladder_idx=5, density=0.3, mid_price=0.2,
        edge=0.1, stake_usd=24.0, fill_price=0.21, shares=110.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind="closed",
        winset_payload_json="[70]", price_trajectory_json="[]"))
    assert db.get_fired_city_days() == {("toronto", "2026-06-10")}
    assert len(db.get_open_bets()) == 1
    assert abs(db.get_open_exposure() - 24.0) < 1e-6
    assert len(db.get_open_fires()) == 1
    assert len(db.get_all_bets_for_fire(fire_id)) == 1


def test_record_account_value_includes_shotgun_exposure(monkeypatch):
    import importlib, tempfile
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(1000.0)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=30.0, n_legs=1, status="open"))
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="c", group_item_title="g",
        side="yes", ladder_idx=5, density=0.3, mid_price=0.2, edge=0.1, stake_usd=30.0,
        fill_price=0.2, shares=150.0, status="open", resolved_outcome=None, pnl=None,
        winset_kind="closed", winset_payload_json="[70]", price_trajectory_json="[]"))
    db.record_account_value()
    hist = db.get_balance_history()
    # latest recorded account value should be cash(1000) + open shotgun exposure(30) = 1030
    assert abs(hist[-1]["amount"] - 1030.0) < 1e-6


def test_update_bet_and_fire(monkeypatch):
    db = _fresh_db(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=1.0, n_legs=1, status="open"))
    bid = db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="m",
        group_item_title="g", side="yes", ladder_idx=5, density=0.3, mid_price=0.2, edge=0.1,
        stake_usd=1.0, fill_price=0.2, shares=5.0, status="open", resolved_outcome=None, pnl=None,
        winset_kind="closed", winset_payload_json="[70]", price_trajectory_json="[]"))
    db.update_bet(bid, dict(status="closed", resolved_outcome="win", pnl=3.0))
    db.update_fire(fid, dict(status="closed"))
    assert db.get_open_bets() == []
    assert db.get_open_fires() == []
    assert db.get_open_exposure() == 0.0
