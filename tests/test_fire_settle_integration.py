import importlib, tempfile


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(2000.0)
    return db


def test_fire_then_settle_end_to_end(monkeypatch):
    db = _fresh(monkeypatch)
    from executor.paper import PaperExecutor
    import shotgun_resolver; importlib.reload(shotgun_resolver)

    ex = PaperExecutor()
    # fully fill every leg at its mid (stub the order-book walk)
    monkeypatch.setattr(ex, "_simulate_fill", lambda token_id, side, size: (0.20, size))

    bets = [
        dict(side="yes", token_id="t1", no_token_id="n1", market_id="mk1",
             sub_market_condition_id="cond_win", group_item_title="69-70F", ladder_idx=5,
             density=0.4, mid_price=0.20, edge=0.20, stake_usd=10.0, liquidity_num=1000,
             winset_kind="closed", winset_payload_json="[70]"),
        dict(side="yes", token_id="t2", no_token_id="n2", market_id="mk2",
             sub_market_condition_id="cond_lose", group_item_title="75-76F", ladder_idx=8,
             density=0.1, mid_price=0.20, edge=-0.10, stake_usd=10.0, liquidity_num=1000,
             winset_kind="closed", winset_payload_json="[76]"),
    ]
    bal0 = db.get_balance()
    fire_id = ex.place_fire(city="toronto", resolution_date="2026-06-10", lead_hours=12.0,
                            center_f=70.0, density_json="[]", budget_usd=50.0, bets=bets)
    assert fire_id > 0
    assert len(db.get_open_bets()) == 2
    # balance debited by total filled stake (10+10)
    assert abs(db.get_balance() - (bal0 - 20.0)) < 1e-6

    # Now settle via Polymarket resolution: cond_win resolves YES (bucket hit),
    # cond_lose resolves NO (bucket miss). Both legs are 'yes' side.
    def fake_resolution(condition_id):
        if condition_id == "cond_win":
            return {"resolved": True, "yes_price": 1.0}   # yes leg wins
        if condition_id == "cond_lose":
            return {"resolved": True, "yes_price": 0.0}   # yes leg loses
        return {"resolved": False}

    bal_before_settle = db.get_balance()
    n = shotgun_resolver.settle_due_fires_polymarket(resolution_fetch=fake_resolution)
    assert n == 2
    # winner: 10 USDC bought at 0.20 -> 50 shares -> pays $50. loser: $0.
    legs = {b["sub_market_condition_id"]: b for b in db.get_all_bets_for_fire(fire_id)}
    assert legs["cond_win"]["resolved_outcome"] == "win"
    assert abs(legs["cond_win"]["pnl"] - (50.0 - 10.0)) < 1e-6
    assert legs["cond_lose"]["resolved_outcome"] == "loss"
    assert abs(legs["cond_lose"]["pnl"] - (-10.0)) < 1e-6
    # balance credited by winner proceeds ($50)
    assert abs(db.get_balance() - (bal_before_settle + 50.0)) < 1e-6
    # fire closed (all legs settled)
    assert db.get_open_fires() == []
    # net over full cycle: started 2000, -20 fire, +50 winner = 2030
    assert abs(db.get_balance() - 2030.0) < 1e-6
