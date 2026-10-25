"""FBR acceptance scenarios (specs/acceptance_tests.md: FBR-S1, S2, X1..X6, X9, X10, plus S8).

X9/X10 use E = 500.60 with the excursion extreme set so r_u is 2.25 (X9) or 2.55 (X10); the
spec's point stands: the nearest midpoint is chosen, and a too-close one is never skipped.
"""

import json

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
    zone,
)

from tournament.strategies.fbr import FBR


def base(vwap=497.0, zones=None, **lv):
    df = frame(price=499.0, vwap=vwap)
    lvl = dict(orh=500.0, orl=494.0, or_complete=True) | lv
    return df, levels(zones or [zone(500.0, 500.0, ("ORH",), "orh")], **lvl)


def s1_bars(df):
    bar(df, 59, 499.75, 499.65, 499.70)
    bar(df, 60, 500.80, 499.90, 500.30)  # excursion
    bar(df, 61, 500.90, 500.00, 500.20)  # X = 500.90
    bar(df, 62, 500.30, 499.60, 499.80)  # return inside; RetLow 499.60
    bar(df, 63, 499.85, 499.40, 499.50)  # trigger


def target(c):
    return json.loads(c["target"])


def test_s1_valid_failed_breakout():
    df, lv = base()
    s1_bars(df)
    c = run(FBR(), df, lv).candidates.iloc[0]
    assert c["trigger_slot"] == 63 and c["underlying_direction"] == "bearish"
    assert (c["entry_reference"], c["invalidation_level"], c["r_u"]) == pytest.approx(
        (499.50, 501.15, 1.65)
    )
    assert target(c)["price"] == pytest.approx(497.0) and target(c)["target_source"] == "vwap"


def test_s2_too_close_vwap_never_falls_through():
    df, lv = base(vwap=498.60)
    s1_bars(df)
    r = run(FBR(), df, lv)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 63)


def test_x1_oversized_excursion_starts_nothing():
    df, lv = base()
    bar(df, 59, 499.75, 499.65, 499.70)
    bar(df, 60, 504.20, 499.40, 499.50)
    r = run(FBR(), df, lv)
    assert "EXCURSION_TOO_FAR" in reasons_at(r, 60) and r.candidates.empty


def test_x2_midpoint_when_vwap_is_behind_entry():
    df, lv = base(vwap=500.5)
    s1_bars(df)
    c = run(FBR(), df, lv).candidates.iloc[0]
    assert target(c)["price"] == pytest.approx(497.0) and target(c)["range_pair_id"] == "OR"


def test_x3_fallback_only_without_a_structural_target():
    df, lv = base(vwap=500.5, zones=[zone(500.0, 500.0, ("PMH",), "pmh")], pmh=500.0, pml=None)
    s1_bars(df)
    c = run(FBR(), df, lv).candidates.iloc[0]
    assert target(c)["target_source"] == "fallback_r"
    assert target(c)["price"] == pytest.approx(499.50 - 1.5 * 1.65)


def test_x4_same_slot_return_and_next_slot_trigger():
    df, lv = base()
    bar(df, 59, 499.75, 499.65, 499.70)
    bar(df, 60, 500.80, 499.80, 499.85)  # excursion and return on one bar
    bar(df, 61, 499.80, 499.50, 499.60)  # below RetLow 499.80
    r = run(FBR(), df, lv)
    assert "RETURN_INSIDE" in reasons_at(r, 60) and r.candidates.iloc[0]["trigger_slot"] == 61


def test_x5_strong_trend_cancels():
    df, lv = base()
    s1_bars(df)
    setcol(df, "disp_30_atr", 2.1, start=61, end=61)
    r = run(FBR(), df, lv)
    assert "REGIME_STRONG_TREND" in reasons_at(r, 61) and r.candidates.empty


def test_x6_no_setup_before_slot_31():
    df, lv = base()
    bar(df, 28, 499.75, 499.65, 499.70)
    bar(df, 29, 500.80, 499.90, 500.30)
    assert all("EXCURSION" not in reasons_at(run(FBR(), df, lv), s) for s in range(15, 31))


def x9_bars(df, x_high):
    bar(df, 59, 500.45, 500.35, 500.40)
    bar(df, 60, 501.20, 500.50, 500.90)
    bar(df, 61, x_high, 500.60, 500.80)
    bar(df, 62, 500.90, 500.20, 500.40)  # return below 500.50; RetLow 500.20
    bar(df, 63, 500.30, 499.40, 499.50)


def x9_setup():
    zs = [zone(500.0, 500.6, ("ORH", "PDH"), "orh-pdh")]
    return base(vwap=500.8, zones=zs, pdh=500.6, pdl=492.6)


def test_x9_nearest_range_pair_midpoint():
    df, lv = x9_setup()
    x9_bars(df, 501.5)
    c = run(FBR(), df, lv).candidates.iloc[0]
    assert target(c)["price"] == pytest.approx(497.0) and target(c)["range_pair_id"] == "OR"
    assert target(c)["target_source"] == "range_midpoint" and c["r_u"] == pytest.approx(2.25)


def test_x10_too_close_midpoint_is_not_skipped():
    df, lv = x9_setup()
    x9_bars(df, 501.8)  # r_u 2.55: OR midpoint 497 too close; PD 496.6 would pass but isn't used
    r = run(FBR(), df, lv)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 63)


def test_s8_bullish_mirror():
    df, lv = base()
    s1_bars(df)
    c = run(FBR(), mirror_frame(df), mirror_levels(lv)).candidates.iloc[0]
    assert c["underlying_direction"] == "bullish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx(
        (1000 - 499.50, 1000 - 501.15)
    )
    assert target(c)["price"] == pytest.approx(1000 - 497.0)


def test_reclaim_invalidates():
    df, lv = base()
    s1_bars(df)
    bar(df, 63, 500.20, 499.70, 500.10)  # closes back above E
    r = run(FBR(), df, lv)
    assert "RECLAIMED" in reasons_at(r, 63) and r.candidates.empty
