import importlib, tempfile


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(2000.0)
    return db


def test_place_fire_records_parent_and_legs(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    # stub the order-book fill: return (fill_price, filled_usdc) fully filled at 0.21
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.21, size))

    bets = [
        dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
             sub_market_condition_id="m1", group_item_title="69-70F", ladder_idx=5,
             density=0.3, mid_price=0.20, edge=0.10, stake_usd=24.0, liquidity_num=1000,
             winset_kind="closed", winset_payload_json="[70]"),
        dict(side="no", token_id="t2", no_token_id="n2", market_id="mk2",
             sub_market_condition_id="m2", group_item_title="71-72F", ladder_idx=6,
             density=0.1, mid_price=0.45, edge=0.10, stake_usd=4.4, liquidity_num=44,
             winset_kind="closed", winset_payload_json="[72]"),
    ]
    bal_before = db.get_balance()
    fire_id = ex.place_fire(
        city="toronto", resolution_date="2026-06-10", lead_hours=12.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, bets=bets)
    assert fire_id > 0
    assert len(db.get_open_bets()) == 2
    assert db.get_fired_city_days() == {("toronto", "2026-06-10")}
    # balance debited by total filled stake (24.0 + 4.4 = 28.4)
    assert abs(db.get_balance() - (bal_before - 28.4)) < 1e-6
    # NO leg bought the no_token; YES leg bought the yes token — shares = filled/fill_price
    bets_db = db.get_all_bets_for_fire(fire_id)
    for b in bets_db:
        assert b["shares"] > 0
        assert b["fill_price"] == 0.21
        assert b["winset_kind"] == "closed"   # passthrough preserved for resolver


def test_place_fire_skips_unfillable_legs(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    # first leg fills, second returns 0 filled (dead book)
    calls = {"n": 0}
    def fake_fill(token_id, side, size):
        calls["n"] += 1
        return (0.20, size) if calls["n"] == 1 else (0.0, 0.0)
    monkeypatch.setattr(ex, "_simulate_fill", fake_fill)
    bets = [
        dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
             sub_market_condition_id="m1", group_item_title="g", ladder_idx=5, density=0.3,
             mid_price=0.2, edge=0.1, stake_usd=10.0, liquidity_num=1000,
             winset_kind="closed", winset_payload_json="[70]"),
        dict(side="yes", token_id="t2", no_token_id="n2", market_id="mk2",
             sub_market_condition_id="m2", group_item_title="g", ladder_idx=6, density=0.2,
             mid_price=0.3, edge=0.1, stake_usd=10.0, liquidity_num=1000,
             winset_kind="closed", winset_payload_json="[72]"),
    ]
    fire_id = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                            density_json="[]", budget_usd=50.0, bets=bets)
    assert len(db.get_open_bets()) == 1   # only the fillable leg recorded


def test_place_fire_returns_zero_when_nothing_fills(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    ex = PaperExecutor()
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.0, 0.0))
    bets = [dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
                 sub_market_condition_id="m1", group_item_title="g", ladder_idx=5, density=0.3,
                 mid_price=0.2, edge=0.1, stake_usd=10.0, liquidity_num=1000,
                 winset_kind="closed", winset_payload_json="[70]")]
    fid = ex.place_fire(city="x", resolution_date="d", lead_hours=1.0, center_f=70.0,
                        density_json="[]", budget_usd=50.0, bets=bets)
    assert fid == 0
    assert db.get_open_bets() == []
