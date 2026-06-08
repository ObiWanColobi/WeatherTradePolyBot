import importlib, tempfile


def _fresh(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("DB_PATH_OVERRIDE", tmp)
    import config; importlib.reload(config)
    import db; importlib.reload(db); db.init_db()
    db.set_balance(2000.0)
    return db


def test_run_fire_pass_fires_city_day_in_window(monkeypatch):
    db = _fresh(monkeypatch)
    import weather_bot; importlib.reload(weather_bot)

    monkeypatch.setattr(weather_bot, "discover_city_days", lambda cities, days_ahead: {
        ("toronto", "2026-06-10"): [dict(
            sub_market_condition_id="m1", market_id="mk1", group_item_title="69-70°F",
            bound_lo_f=69.0, bound_hi_f=70.0, is_open_tail=0, mid_price=0.20,
            best_bid=0.19, best_ask=0.21, volume_24h=500, liquidity_num=1000,
            token_id="t1", no_token_id="n1", city="toronto", resolution_date="2026-06-10")]})
    monkeypatch.setattr(weather_bot, "in_fire_window", lambda c, d, fire_window_hours, now=None: True)
    monkeypatch.setattr(weather_bot, "hours_to_close", lambda c, d, now=None: 12.0)
    monkeypatch.setattr(weather_bot, "_ensemble_fetch", lambda lat, lon, tz: [
        {"date": "2026-06-10", "member_temps": [21.0]*10}])
    monkeypatch.setattr(weather_bot, "_coords_for", lambda city: {"lat":43.7,"lon":-79.4,"tz":"America/Toronto"})
    monkeypatch.setattr(weather_bot._executor, "_simulate_fill", lambda t, s, sz: (0.21, sz))

    weather_bot.run_fire_pass()
    assert db.get_fired_city_days() == {("toronto", "2026-06-10")}


def test_run_fire_pass_skips_already_fired(monkeypatch):
    db = _fresh(monkeypatch)
    db.insert_fire(dict(fired_at_utc="t", city="toronto", resolution_date="2026-06-10",
        lead_hours=12.0, center_f=70.0, density_json="[]", budget_usd=50.0,
        total_staked_usd=1.0, n_legs=1, status="open"))
    import weather_bot; importlib.reload(weather_bot)
    monkeypatch.setattr(weather_bot, "discover_city_days", lambda cities, days_ahead: {
        ("toronto", "2026-06-10"): []})
    monkeypatch.setattr(weather_bot, "in_fire_window", lambda *a, **k: True)
    weather_bot.run_fire_pass()
    assert len(db.get_open_fires()) == 1   # no NEW fire created


def test_run_fire_pass_respects_exposure_cap(monkeypatch):
    db = _fresh(monkeypatch)
    db.set_balance(100.0)
    # pre-load open exposure near the 80% cap by inserting a fire+bet of $79
    fid = db.insert_fire(dict(fired_at_utc="t", city="seoul", resolution_date="2026-06-09",
        lead_hours=12.0, center_f=70.0, density_json="[]", budget_usd=50.0,
        total_staked_usd=79.0, n_legs=1, status="open"))
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="c", group_item_title="g",
        side="yes", ladder_idx=5, density=0.3, mid_price=0.2, edge=0.1, stake_usd=79.0,
        fill_price=0.2, shares=395.0, status="open", resolved_outcome=None, pnl=None,
        winset_kind="closed", winset_payload_json="[70]", price_trajectory_json="[]"))
    import weather_bot; importlib.reload(weather_bot)
    monkeypatch.setattr(weather_bot, "discover_city_days", lambda cities, days_ahead: {
        ("toronto", "2026-06-10"): [dict(sub_market_condition_id="m1", market_id="mk1",
            group_item_title="69-70°F", bound_lo_f=69.0, bound_hi_f=70.0, is_open_tail=0,
            mid_price=0.20, best_bid=0.19, best_ask=0.21, volume_24h=500, liquidity_num=1000,
            token_id="t1", no_token_id="n1", city="toronto", resolution_date="2026-06-10")]})
    monkeypatch.setattr(weather_bot, "in_fire_window", lambda *a, **k: True)
    monkeypatch.setattr(weather_bot, "hours_to_close", lambda c, d, now=None: 12.0)
    monkeypatch.setattr(weather_bot, "_ensemble_fetch", lambda lat, lon, tz: [{"date":"2026-06-10","member_temps":[21.0]*10}])
    monkeypatch.setattr(weather_bot, "_coords_for", lambda city: {"lat":43.7,"lon":-79.4,"tz":"America/Toronto"})
    monkeypatch.setattr(weather_bot._executor, "_simulate_fill", lambda t,s,sz: (0.21, sz))
    # exposure 79 >= 0.80*100=80? no, 79<80 — so set cap lower to force the skip:
    import config
    config.SHOTGUN["portfolio_exposure_cap_pct"] = 0.50  # cap = $50; exposure $79 already over
    weather_bot.run_fire_pass()
    assert ("toronto","2026-06-10") not in db.get_fired_city_days()   # blocked by cap


def test_run_resolve_pass_settles(monkeypatch):
    db = _fresh(monkeypatch)
    fid = db.insert_fire(dict(fired_at_utc="t", city="x", resolution_date="d", lead_hours=1.0,
        center_f=70.0, density_json="[]", budget_usd=50.0, total_staked_usd=5.0, n_legs=1, status="open"))
    db.insert_bet(dict(fire_id=fid, market_id="mk", sub_market_condition_id="cond1", group_item_title="g",
        side="yes", ladder_idx=5, density=0.3, mid_price=0.2, edge=0.1, stake_usd=5.0,
        fill_price=0.2, shares=25.0, status="open", resolved_outcome=None, pnl=None,
        winset_kind="closed", winset_payload_json="[70]", price_trajectory_json="[]"))
    import weather_bot; importlib.reload(weather_bot)
    monkeypatch.setattr(weather_bot, "_resolution_fetch", lambda cid: {"resolved": True, "yes_price": 1.0})
    weather_bot.run_resolve_pass()
    assert db.get_open_fires() == []
