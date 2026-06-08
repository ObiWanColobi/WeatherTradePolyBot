"""Reconciliation: live plan_fire() must select the same bets as calling the
validated research math (build_bucket_bets) directly. Guards the live row-build
against drifting from the backtest. Liquidity cap OFF (frac=0) for exact match."""
import json
from shotgun_strategy import plan_fire, ShotgunConfig
from shotgun.bets import build_bucket_bets, compute_winset, winset_density
from shotgun.bucket_boundaries import build_ladder, daily_max_to_bucket_idx
from shotgun.forecast import members_f_for_date, build_density


def _ensemble(lat, lon, tz):
    return [{"date": "2026-06-10", "member_temps": [21.0]*7 + [22.0]*3}]  # °C


def _buckets():
    return [
        dict(sub_market_condition_id="m1", market_id="mk1", group_item_title="69-70°F",
             bound_lo_f=69.0, bound_hi_f=70.0, is_open_tail=0, mid_price=0.15,
             best_bid=0.14, best_ask=0.16, volume_24h=500, liquidity_num=1000,
             token_id="t1", no_token_id="n1"),
        dict(sub_market_condition_id="m2", market_id="mk2", group_item_title="71-72°F",
             bound_lo_f=71.0, bound_hi_f=72.0, is_open_tail=0, mid_price=0.30,
             best_bid=0.29, best_ask=0.31, volume_24h=500, liquidity_num=1000,
             token_id="t2", no_token_id="n2"),
        dict(sub_market_condition_id="m3", market_id="mk3", group_item_title="73-74°F",
             bound_lo_f=73.0, bound_hi_f=74.0, is_open_tail=0, mid_price=0.40,
             best_bid=0.39, best_ask=0.41, volume_24h=500, liquidity_num=1000,
             token_id="t3", no_token_id="n3"),
    ]


def _keyset(bets):
    return sorted((b["sub_market_condition_id"], b["side"], round(b["stake_usd"], 6)) for b in bets)


def test_plan_fire_matches_direct_build_bucket_bets():
    cfg = ShotgunConfig(edge_threshold=0.05, mass_core_frac=0.9,
                        budget_per_city_day=50.0, per_bucket_liq_cap_frac=0.0)
    coords = {"lat": 43.7, "lon": -79.4, "tz": "America/Toronto"}
    live_bets, live_center, live_density = plan_fire(
        "toronto", "2026-06-10", _buckets(), cfg, coords, _ensemble)

    # Direct path: build the same rows manually + call build_bucket_bets.
    members_f = members_f_for_date(_ensemble(0, 0, "x"), "2026-06-10")
    center_f, density = build_density(members_f)
    ladder = build_ladder(center_f)
    rows = []
    for m in _buckets():
        ws = compute_winset(m["bound_lo_f"], m["bound_hi_f"], False)
        d = winset_density(ws, ladder, density)
        rep = ws[1][len(ws[1]) // 2]
        idx = daily_max_to_bucket_idx(rep, ladder)
        rows.append(dict(ladder_idx=idx, density=float(d), mid_price=m["mid_price"],
            sub_market_condition_id=m["sub_market_condition_id"], group_item_title=m["group_item_title"],
            winset_kind=ws[0], winset_payload_json=json.dumps(ws[1]), center_f=center_f,
            bucket_idx=idx, edge=float(d)-m["mid_price"], volume_24h=500,
            best_bid=m["best_bid"], best_ask=m["best_ask"], liquidity_num=m["liquidity_num"],
            token_id=m["token_id"], no_token_id=m["no_token_id"], market_id=m["market_id"],
            city="toronto", resolution_date="2026-06-10"))
    direct_bets = build_bucket_bets(rows, list(density), "dist_yes_no", 0.9, 0.05, 0.05, 0.50, "weighted", 50.0)

    assert _keyset(live_bets) == _keyset(direct_bets)
    # provenance returned by plan_fire matches the direct computation
    assert abs(live_center - center_f) < 1e-9
    assert live_density == density


def test_plan_fire_provenance_returned_on_empty():
    # no ensemble -> ([], None, None)
    cfg = ShotgunConfig()
    bets, center, density = plan_fire("toronto", "2099-01-01", _buckets(), cfg,
                                     {"lat":43.7,"lon":-79.4,"tz":"America/Toronto"},
                                     lambda *a: [])
    assert bets == [] and center is None and density is None
