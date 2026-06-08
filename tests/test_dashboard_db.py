import importlib, tempfile


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    return db


def test_get_fires_with_rollup(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="2026-06-10T12:00:00Z", city="toronto",
        resolution_date="2026-06-10", lead_hours=12.0, center_f=70.0, density_json="[]",
        budget_usd=50.0, total_staked_usd=10.0, n_legs=2, status="open"))
    db.insert_bet(dict(fire_id=fid, market_id="mk1", sub_market_condition_id="m1",
        group_item_title="69-70F", side="yes", ladder_idx=5, density=0.3, mid_price=0.2,
        edge=0.1, stake_usd=6.0, fill_price=0.2, shares=30.0, status="closed",
        resolved_outcome="win", pnl=24.0, winset_kind="closed", winset_payload_json="[70]",
        price_trajectory_json="[]"))
    db.insert_bet(dict(fire_id=fid, market_id="mk2", sub_market_condition_id="m2",
        group_item_title="71-72F", side="yes", ladder_idx=6, density=0.1, mid_price=0.3,
        edge=0.0, stake_usd=4.0, fill_price=0.3, shares=13.0, status="open",
        resolved_outcome=None, pnl=None, winset_kind="closed", winset_payload_json="[72]",
        price_trajectory_json="[]"))
    rows = db.get_fires_with_rollup()
    assert len(rows) == 1
    r = rows[0]
    assert r["city"] == "toronto" and r["legs"] == 2
    assert abs(r["staked"] - 10.0) < 1e-6
    assert abs(r["pnl"] - 24.0) < 1e-6   # only the closed winner has pnl
    assert r["wins"] == 1
    assert r["open_legs"] == 1
