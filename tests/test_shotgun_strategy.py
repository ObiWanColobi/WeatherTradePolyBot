from shotgun_strategy import plan_fire, ShotgunConfig


def _fake_ensemble(lat, lon, tz):
    # 10 members tightly around 70°F (21.1°C) so density concentrates mid-ladder
    return [{"date": "2026-06-10", "member_temps": [21.0]*10}]


def _buckets():
    return [
        dict(sub_market_condition_id="m1", group_item_title="69-70°F",
             bound_lo_f=69.0, bound_hi_f=70.0, is_open_tail=0,
             mid_price=0.20, best_bid=0.19, best_ask=0.21, liquidity_num=1000, volume_24h=500,
             token_id="t1", no_token_id="n1", market_id="mk1"),
        dict(sub_market_condition_id="m2", group_item_title="71-72°F",
             bound_lo_f=71.0, bound_hi_f=72.0, is_open_tail=0,
             mid_price=0.45, best_bid=0.44, best_ask=0.46, liquidity_num=44, volume_24h=500,
             token_id="t2", no_token_id="n2", market_id="mk2"),
    ]


def test_plan_fire_returns_sized_bets():
    cfg = ShotgunConfig(edge_threshold=0.05, mass_core_frac=0.9, budget_per_city_day=50.0,
                        per_bucket_liq_cap_frac=0.10)
    bets = plan_fire(
        city="toronto", resolution_date="2026-06-10", buckets=_buckets(),
        cfg=cfg, coords={"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"},
        ensemble_fetch=_fake_ensemble,
    )
    assert len(bets) >= 1
    for b in bets:
        assert b["side"] in ("yes", "no")
        assert b["stake_usd"] > 0
        assert "sub_market_condition_id" in b
    for b in bets:
        if b.get("liquidity_num") == 44:
            assert b["stake_usd"] <= 4.4 + 1e-6   # liquidity cap applied


def test_plan_fire_empty_when_no_ensemble():
    cfg = ShotgunConfig()
    bets = plan_fire(
        city="toronto", resolution_date="2099-01-01", buckets=_buckets(), cfg=cfg,
        coords={"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"},
        ensemble_fetch=lambda *a: [],
    )
    assert bets == []


def test_plan_fire_empty_when_no_edge():
    cfg = ShotgunConfig(edge_threshold=0.99)
    bets = plan_fire(
        city="toronto", resolution_date="2026-06-10", buckets=_buckets(), cfg=cfg,
        coords={"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"},
        ensemble_fetch=_fake_ensemble,
    )
    assert bets == []


def test_plan_fire_skips_low_volume_buckets():
    cfg = ShotgunConfig(edge_threshold=0.05, mass_core_frac=0.9, vol_min=10000)
    bets = plan_fire(
        city="toronto", resolution_date="2026-06-10", buckets=_buckets(), cfg=cfg,
        coords={"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"},
        ensemble_fetch=_fake_ensemble,
    )
    assert bets == []   # both buckets have volume_24h=500 < 10000
