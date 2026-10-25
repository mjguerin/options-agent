"""VPC acceptance scenarios (specs/acceptance_tests.md: VPC-S1..S4, X2..X8, plus S8, re-anchor)."""

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
    swing,
)

from tournament.strategies.vpc import VPC


def base():
    df = frame(price=503.5, vwap=502.0, regime="TREND_UP")
    setcol(df, "session_vwap", 502.1, start=101)
    return df


def s1_bars(df, h=505.0):
    bar(df, 100, h, h - 0.5, h - 0.2)
    swing(df, "high", 100, h)  # H confirmed at 102
    bar(df, 101, 504.6, 504.0, 504.2)
    bar(df, 102, 504.0, 503.5, 503.6)
    bar(df, 103, 503.6, 503.2, 503.3)
    bar(df, 104, 503.3, 502.6, 502.8)  # pullback (<= VWAP + 1.0)
    bar(df, 105, 503.0, 502.5, 502.7)
    bar(df, 106, 502.9, 502.4, 502.6)  # L pivot
    bar(df, 107, 502.95, 502.5, 502.8)
    bar(df, 108, 503.0, 502.6, 502.9)  # c; P = 503.00
    swing(df, "low", 106, 502.4)


def trigger(df, slot):
    bar(df, slot, 503.2, 502.9, 503.10)


def flat(df, start, end, high=503.0, low=502.6, close=502.9):
    for s in range(start, end + 1):
        bar(df, s, high, low, close)


def test_s1_valid_pullback_continuation():
    df = base()
    s1_bars(df)
    trigger(df, 109)
    c = run(VPC(), df).candidates.iloc[0]
    assert c["trigger_slot"] == 109 and c["underlying_direction"] == "bullish"
    assert (c["entry_reference"], c["invalidation_level"], c["r_u"]) == pytest.approx(
        (503.10, 502.15, 0.95)
    )
    assert json.loads(c["target"])["price"] == pytest.approx(505.0)


def test_s2_rr_too_low():
    df = base()
    s1_bars(df, h=503.8)
    setcol(df, "session_vwap", 501.5, start=100, end=100)  # H still extended: 2.30 A
    for s, (hi, lo, cl) in {
        101: (503.7, 503.3, 503.4),
        102: (503.5, 503.2, 503.3),
        103: (503.4, 503.15, 503.2),
    }.items():
        bar(df, s, hi, lo, cl)
    trigger(df, 109)
    r = run(VPC(), df)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 109)


def test_s3_trigger_window_boundary():
    df = base()
    s1_bars(df)
    flat(df, 109, 122)
    trigger(df, 123)  # c + 15
    assert run(VPC(), df).candidates.iloc[0]["trigger_slot"] == 123
    df = base()
    s1_bars(df)
    flat(df, 109, 123)
    trigger(df, 124)
    r = run(VPC(), df)
    assert r.candidates.empty and "TRIGGER_TIMEOUT" in reasons_at(r, 123)


def test_s4_pullback_too_deep():
    df = base()
    s1_bars(df)
    bar(df, 105, 502.8, 501.4, 501.5)  # close < VWAP - 0.5
    r = run(VPC(), df)
    assert "PULLBACK_TOO_DEEP" in reasons_at(r, 105) and r.candidates.empty


def test_x2_pullback_before_h_confirms_is_reconstructed():
    df = base()
    s1_bars(df)
    bar(df, 101, 504.6, 503.0, 503.4)  # touches the zone before H confirms at 102
    r = run(VPC(), df)
    assert "PULLBACK_RECONSTRUCTED" in reasons_at(r, 102)


def undercut(df):
    flat(df, 109, 111)
    bar(df, 112, 503.0, 502.35, 502.9)  # low < L, close still fine


def test_x3_undercut_invalidates_regardless_of_close():
    df = base()
    s1_bars(df)
    undercut(df)
    r = run(VPC(), df)
    assert "SUPPORT_UNDERCUT" in reasons_at(r, 112)
    assert (
        r.evaluations.set_index("slot")
        .loc[113, "setup_state"]
        .startswith("bullish:AWAITING_REPLACEMENT_L")
    )


def test_x6_replacement_from_the_undercut_bar_pivot():
    df = base()
    s1_bars(df)
    undercut(df)
    bar(df, 113, 503.0, 502.5, 502.8)
    bar(df, 114, 503.0, 502.6, 502.9)
    swing(df, "low", 112, 502.35)  # pivot = the undercut bar, confirmed at 114
    trigger(df, 115)
    r = run(VPC(), df)
    assert "SUPPORT_CONFIRMED" in reasons_at(r, 114)
    c = r.candidates.iloc[0]
    assert c["trigger_slot"] == 115 and c["invalidation_level"] == pytest.approx(502.35 - 0.25)
    assert json.loads(c["target"])["price"] == pytest.approx(505.0)  # same H


def test_x7_older_pivot_is_never_reused():
    df = base()
    s1_bars(df)
    undercut(df)
    flat(df, 113, 120)
    swing(df, "low", 111, 502.45)  # pivot 111 < undercut slot 112, confirmed at 113
    r = run(VPC(), df)
    assert "SUPPORT_CONFIRMED" not in reasons_at(r, 113)
    assert (
        r.evaluations.set_index("slot")
        .loc[114, "setup_state"]
        .startswith("bullish:AWAITING_REPLACEMENT_L")
    )


def test_x8_regime_loss_drops_context_and_retires_h():
    df = base()
    s1_bars(df)
    undercut(df)
    setcol(df, "regime", "NEUTRAL", start=113, end=113)
    bar(df, 130, 505.0, 504.5, 504.8)
    swing(df, "high", 130, 505.0)  # equal to the retired H
    r = run(VPC(), df)
    assert any("REPLACEMENT_DROPPED_REGIME_LOST" in x for x in reasons_at(r, 113))
    assert "H_SELECTED" not in reasons_at(r, 132)


def test_x4_regime_loss_in_supported():
    df = base()
    s1_bars(df)
    setcol(df, "regime", "NEUTRAL", start=109, end=109)
    r = run(VPC(), df)
    assert "REGIME_LOST" in reasons_at(r, 109)


def test_x5_after_stop_out_h_must_be_strictly_higher():
    df = base()
    s1_bars(df)
    trigger(df, 109)
    bar(df, 110, 502.5, 501.9, 502.0)  # close <= invalidation 502.15
    bar(df, 130, 505.0, 504.5, 504.8)
    swing(df, "high", 130, 505.0)  # equal: retired
    bar(df, 150, 505.5, 505.0, 505.3)
    swing(df, "high", 150, 505.5)  # strictly higher: usable
    r = run(VPC(), df)
    assert r.reference_positions.iloc[0]["exit_primary_reason"] == "INVALIDATION"
    assert "H_SELECTED" not in reasons_at(r, 132) and "H_SELECTED" in reasons_at(r, 152)


def test_reanchor_to_higher_h_in_extended():
    df = base()
    s1_bars(df)
    bar(df, 104, 505.6, 503.2, 505.3)
    bar(df, 105, 505.4, 503.3, 504.0)
    swing(df, "high", 104, 505.6)  # confirmed at 106, before any pullback
    for s in (106, 107, 108):
        bar(df, s, 504.0, 503.3, 503.5)
    r = run(VPC(), df)
    assert "H_REANCHORED" in reasons_at(r, 106)


def test_s8_bearish_mirror():
    df = base()
    s1_bars(df)
    trigger(df, 109)
    c = run(VPC(), mirror_frame(df), mirror_levels(levels([]))).candidates.iloc[0]
    assert c["underlying_direction"] == "bearish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx(
        (1000 - 503.10, 1000 - 502.15)
    )
    assert json.loads(c["target"])["price"] == pytest.approx(1000 - 505.0)
