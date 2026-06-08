from shotgun_discovery import buckets_from_event, discover_city_days
import markets.polymarket as polymarket


def test_buckets_from_event_parses_bounds_and_prices():
    event = {
        "slug": "highest-temperature-in-toronto-on-june-10-2026",
        "endDate": "2026-06-10T12:00:00Z",
        "markets": [
            {"id": "1", "conditionId": "m1", "groupItemTitle": "69-70°F",
             "bestBid": 0.19, "bestAsk": 0.21, "volume24hr": 500, "liquidityNum": 1000,
             "clobTokenIds": '["t1","n1"]'},
        ],
    }
    out = buckets_from_event(event, city="toronto", resolution_date="2026-06-10")
    assert len(out) == 1
    b = out[0]
    assert b["bound_lo_f"] == 69.0 and b["bound_hi_f"] == 70.0
    assert abs(b["mid_price"] - 0.20) < 1e-6
    assert b["token_id"] == "t1" and b["no_token_id"] == "n1"
    assert b["sub_market_condition_id"] == "m1"


def test_buckets_from_event_handles_missing_book():
    event = {"slug": "s", "endDate": "z", "markets": [
        {"id": "2", "conditionId": "m2", "groupItemTitle": "71-72°F",
         "volume24hr": 0, "clobTokenIds": '["t2","n2"]'}]}  # no bid/ask
    out = buckets_from_event(event, "x", "d")
    assert out[0]["mid_price"] is None   # no priceable book


def test_discover_city_days_groups_by_event(monkeypatch):
    fake_event = {
        "slug": "highest-temperature-in-toronto-on-june-10-2026",
        "endDate": "2026-06-10T12:00:00Z",
        "markets": [
            {"id": "1", "conditionId": "m1", "groupItemTitle": "69-70°F",
             "bestBid": 0.19, "bestAsk": 0.21, "volume24hr": 500, "liquidityNum": 1000,
             "clobTokenIds": '["t1","n1"]'},
            {"id": "2", "conditionId": "m2", "groupItemTitle": "71-72°F",
             "bestBid": 0.30, "bestAsk": 0.32, "volume24hr": 500, "liquidityNum": 1000,
             "clobTokenIds": '["t2","n2"]'},
        ],
    }
    monkeypatch.setattr(polymarket, "iter_weather_events",
                        lambda cities, days_ahead=2, kinds=("highest",): iter([fake_event]))
    groups = discover_city_days(["toronto"], days_ahead=2)
    assert ("toronto", "2026-06-10") in groups
    assert len(groups[("toronto", "2026-06-10")]) == 2
