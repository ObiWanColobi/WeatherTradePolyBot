def test_shotgun_package_imports():
    from shotgun.bucket_boundaries import build_ladder, daily_max_to_bucket_idx
    from shotgun.bucket_density import ensemble_to_density, deterministic_to_density
    ladder = build_ladder(70.0)
    assert len(ladder) == 11

def test_ensemble_density_sums_to_one():
    from shotgun.bucket_boundaries import build_ladder
    from shotgun.bucket_density import ensemble_to_density
    ladder = build_ladder(70.0)
    d = ensemble_to_density([68.0, 70.0, 71.0, 72.0], ladder)
    assert len(d) == 11
    assert abs(sum(d) - 1.0) < 1e-9

def test_research_db_shim_still_works():
    import sys, os
    rd = os.path.join(os.getcwd(), "research_db")
    if rd not in sys.path:
        sys.path.insert(0, rd)
    import importlib
    bb = importlib.import_module("bucket_boundaries")
    bd = importlib.import_module("bucket_density")
    assert hasattr(bb, "build_ladder")
    assert hasattr(bd, "ensemble_to_density")
