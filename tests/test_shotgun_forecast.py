from shotgun.forecast import members_f_for_date, build_density


def test_members_f_for_date_converts_c_to_f_and_filters_date():
    ens = [
        {"date": "2026-06-10", "member_temps": [20.0, 21.0]},   # °C
        {"date": "2026-06-11", "member_temps": [25.0]},
    ]
    out = members_f_for_date(ens, "2026-06-10")
    assert out == [68.0, 69.8]   # 20C=68F, 21C=69.8F


def test_members_f_for_date_missing_returns_empty():
    assert members_f_for_date([{"date": "2026-06-10", "member_temps": [20.0]}], "2026-06-12") == []


def test_build_density_returns_center_and_11_vector():
    members_f = [68.0, 69.8, 70.0, 71.0]
    center_f, density = build_density(members_f)
    assert abs(center_f - sum(members_f)/len(members_f)) < 1e-6
    assert len(density) == 11
    assert abs(sum(density) - 1.0) < 1e-6


def test_build_density_empty_returns_none():
    center_f, density = build_density([])
    assert center_f is None and density is None
