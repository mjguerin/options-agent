"""VMR acceptance scenarios (specs/acceptance_tests.md: VMR-S1, S2, X1..X8, plus S8)."""

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

from tournament.strategies.base import ReferenceExitEvent
from tournament.strategies.vmr import VMR


def base():
    return frame(price=500.0, vwap=500.0, regime="RANGE")


def s1_bars(df, trigger_close=505.90, trigger_low=505.80):
    bar(df, 99, 505.85, 505.70, 505.80)  # d 5.8
    bar(df, 100, 506.15, 506.00, 506.10)  # d 6.1: episode start
    bar(df, 101, 506.50, 506.00, 506.20)  # pivot: S = 506.50, S_low = 506.00
    bar(df, 102, 506.30, 505.95, 506.00)
    bar(df, 103, 506.10, trigger_low, trigger_close)
    swing(df, "high", 101, 506.50)  # confirmed at 103


def test_s1_trigger_on_confirmation_slot():
    df = base()
    s1_bars(df)
    c = run(VMR(), df).candidates.iloc[0]
    assert c["trigger_slot"] == 103 and c["underlying_direction"] == "bearish"
    assert (c["entry_reference"], c["invalidation_level"], c["r_u"]) == pytest.approx(
        (505.90, 507.00, 1.10)
    )
    assert json.loads(c["target"])["price"] == pytest.approx(501.475)


def test_s2_rr_too_low():
    df = base()
    s1_bars(df, trigger_close=500.80, trigger_low=500.70)
    r = run(VMR(), df)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 103)


def test_x1_new_extreme_returns_to_extended():
    df = base()
    s1_bars(df, trigger_close=506.05, trigger_low=506.00)  # no trigger on c
    bar(df, 104, 506.70, 506.10, 506.60)
    r = run(VMR(), df)
    assert "NEW_EXTREME" in reasons_at(r, 104) and r.candidates.empty


def test_x2_leaving_range_cancels():
    df = base()
    s1_bars(df)
    setcol(df, "regime", "NEUTRAL", start=101, end=101)
    r = run(VMR(), df)
    assert "REGIME_EXCLUDED" in reasons_at(r, 101) and r.candidates.empty


def later_episode(df, start, f=1):
    """A fresh stretch episode beginning at `start` (bearish if f=+1, bullish if f=-1)."""
    p0, p1 = 500 + f * 5.8, 500 + f * 6.1
    bar(df, start - 1, p0 + 0.05, p0 - 0.05, p0)
    bar(df, start, p1 + 0.05, p1 - 0.05, p1)


def test_x3_brake_after_invalidation_blocks_that_direction_only():
    df = base()
    s1_bars(df)
    bar(df, 104, 507.20, 506.50, 507.10)  # close above the invalidation 507.00
    later_episode(df, 150, f=1)
    later_episode(df, 200, f=-1)
    w = VMR()
    r = run(w, df)
    assert r.reference_positions.iloc[0]["exit_primary_reason"] == "INVALIDATION"
    assert "STRETCH" not in reasons_at(r, 150)
    assert "STRETCH" in reasons_at(r, 200)


def test_x4_loss_without_invalidation_does_not_brake():
    df = base()
    s1_bars(df)
    setcol(df, "close", 505.70, start=104, end=140)  # stalls: follow-through exit, no invalidation
    setcol(df, "high", 505.80, start=104, end=140)
    setcol(df, "low", 505.60, start=104, end=140)
    later_episode(df, 170, f=1)
    r = run(VMR(), df)
    assert r.reference_positions.iloc[0]["exit_primary_reason"] == "FOLLOW_THROUGH"
    assert "STRETCH" in reasons_at(r, 170)


def test_x5_no_setup_before_slot_60():
    df = base()
    later_episode(df, 59)
    assert "STRETCH" not in reasons_at(run(VMR(), df), 59)


def test_x6_triggered_invalidation_fires_brake_whatever_the_primary():
    w = VMR()
    w.reset_session("s")
    w.on_reference_exit(
        ReferenceExitEvent(
            "c", "x", "bearish", 380, "SESSION_CUTOFF", ("SESSION_CUTOFF", "INVALIDATION")
        )
    )
    assert w.braked == {1: True, -1: False}


def test_x7_pre_entry_invalidation_does_not_brake():
    df = base()
    s1_bars(df)
    setcol(df, "regime", "NEUTRAL", start=101, end=101)
    w = VMR()
    run(w, df)
    assert w.braked == {1: False, -1: False}


def test_x8_target_exit_does_not_brake():
    df = base()
    s1_bars(df)  # the default 500.00 close on slot 104 reaches the target
    w = VMR()
    r = run(w, df)
    assert r.reference_positions.iloc[0]["exit_primary_reason"] == "TARGET" and w.braked[1] is False


def test_s8_bullish_mirror():
    df = base()
    s1_bars(df)
    c = run(VMR(), mirror_frame(df), mirror_levels(levels([]))).candidates.iloc[0]
    assert c["underlying_direction"] == "bullish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx(
        (1000 - 505.90, 1000 - 507.00)
    )
    assert json.loads(c["target"])["price"] == pytest.approx(1000 - 501.475)
