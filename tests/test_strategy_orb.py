"""ORB acceptance scenarios (specs/acceptance_tests.md: ORB-S1..S3, X1..X5, plus S7/S8)."""

import pytest
from strategy_fixtures import (
    bar,
    frame,
    levels,
    mirror_frame,
    mirror_levels,
    reasons_at,
    run,
    setcol,
)

from tournament.strategies.orb import ORB


def base():
    df = frame(price=497.0, vwap=497.0)
    lv = levels([], orh=500.0, orl=494.0, or_complete=True)
    return df, lv


def breakout(df, slot=20, close=500.40, prev=499.90, high=500.50, low=499.80, rvol=1.6):
    bar(df, slot - 1, max(prev, 497) + 0.05, min(prev, 497) - 0.05, prev)
    bar(df, slot, high, low, close, rvol=rvol)


def test_s1_valid_breakout():
    df, lv = base()
    breakout(df)
    r = run(ORB(), df, lv)
    c = r.candidates.iloc[0]
    assert c["trigger_slot"] == 20 and c["underlying_direction"] == "bullish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx((500.40, 498.50))
    assert c["r_u"] == pytest.approx(1.90)
    assert eval(c["target"])["price"] == pytest.approx(506.00)


def test_s2_target_exhausted():
    df, lv = base()
    breakout(df, close=506.25, high=506.30, low=506.10)  # clearly in the upper half
    r = run(ORB(), df, lv)
    assert r.candidates.empty and "TARGET_EXHAUSTED" in reasons_at(r, 20)


def test_s3_last_entry_boundary():
    df, lv = base()
    breakout(df, slot=120)
    assert run(ORB(), df, lv).candidates.iloc[0]["trigger_slot"] == 120
    df, lv = base()
    breakout(df, slot=121)
    assert run(ORB(), df, lv).candidates.empty


def test_x1_narrow_range_ineligible_all_day_despite_atr_change():
    df, lv = base()
    lv.orh, lv.orl = 500.0, 497.5  # W_A = 2.5
    setcol(df, "a_cur", 0.5, start=16)  # a later ATR change doesn't matter
    breakout(df)
    r = run(ORB(), df, lv)
    assert r.candidates.empty and "OR_TOO_NARROW" in reasons_at(r, 20)


def test_x2_no_late_confirmation():
    df, lv = base()
    breakout(df, rvol=1.2)
    bar(df, 21, 500.60, 500.30, 500.50, rvol=2.0)  # still above, but not a new crossing
    assert run(ORB(), df, lv).candidates.empty


def test_x3_zero_range_bar_fails_confirmation():
    df, lv = base()
    breakout(df, close=500.40, high=500.40, low=500.40)
    assert run(ORB(), df, lv).candidates.empty


def test_x4_follow_through_is_post_entry():
    df, lv = base()
    breakout(df)
    for s in range(21, 31):
        bar(df, s, 501.0, 500.0, 500.6)  # never closes at or above 501.35
    rp = run(ORB(), df, lv).reference_positions.iloc[0]
    assert (rp["exit_slot"], rp["exit_primary_reason"]) == (30, "FOLLOW_THROUGH")


def test_x5_rr_too_low():
    df, lv = base()
    breakout(df, close=502.40, prev=500.05, high=502.50, low=502.20)
    r = run(ORB(), df, lv)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 20)


def test_s7_one_candidate_per_direction():
    df, lv = base()
    breakout(df)
    bar(df, 21, 498.4, 497.9, 498.0)  # invalidation exit at slot 21
    breakout(df, slot=40)  # a later valid bullish crossing
    r = run(ORB(), df, lv)
    assert list(r.candidates["trigger_slot"]) == [20]


def test_s8_bearish_mirror():
    df, lv = base()
    breakout(df)
    r = run(ORB(), mirror_frame(df), mirror_levels(lv))
    c = r.candidates.iloc[0]
    assert c["underlying_direction"] == "bearish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx(
        (1000 - 500.40, 1000 - 498.50)
    )
    assert eval(c["target"])["price"] == pytest.approx(1000 - 506.00)


def test_incomplete_or_is_ineligible():
    df, lv = base()
    lv.or_complete = False
    breakout(df)
    r = run(ORB(), df, lv)
    assert r.candidates.empty and "OR_INCOMPLETE" in reasons_at(r, 20)
