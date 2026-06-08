def test_config_has_shotgun_block():
    import config
    s = config.SHOTGUN
    assert s["fire_window_hours"] == 12.0
    assert s["budget_per_city_day"] == 50.0
    assert s["edge_threshold"] == 0.12
    assert s["mass_core_frac"] == 0.50
    assert s["portfolio_exposure_cap_pct"] == 0.80
    assert config.TRADING_MODE in ("paper", "live")
    assert config.PAPER_STARTING_BALANCE > 0
    assert isinstance(s["cities"], list) and len(s["cities"]) > 0


def test_config_db_path_env_override(monkeypatch):
    import importlib
    monkeypatch.setenv("DB_PATH_OVERRIDE", "/tmp/foo.db")
    import config; importlib.reload(config)
    assert config.DB_PATH == "/tmp/foo.db"
