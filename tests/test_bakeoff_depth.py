import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bakeoff.harness.depth import walk_book

def test_walk_book_single_level_full_fill():
    r = walk_book([(0.30, 100.0)], shares_wanted=50.0)
    assert r["filled_shares"] == pytest.approx(50.0)
    assert r["avg_price"] == pytest.approx(0.30)
    assert r["unfilled_shares"] == pytest.approx(0.0)

def test_walk_book_walks_to_second_level():
    r = walk_book([(0.30, 40.0), (0.32, 100.0)], shares_wanted=60.0)
    assert r["filled_shares"] == pytest.approx(60.0)
    # 40 @ 0.30 + 20 @ 0.32 = 12.0 + 6.4 = 18.4 over 60 shares
    assert r["avg_price"] == pytest.approx(18.4 / 60.0)
    assert r["unfilled_shares"] == pytest.approx(0.0)

def test_walk_book_runs_out_of_depth():
    r = walk_book([(0.30, 40.0)], shares_wanted=100.0)
    assert r["filled_shares"] == pytest.approx(40.0)
    assert r["unfilled_shares"] == pytest.approx(60.0)
