"""BRT acceptance scenarios (specs/acceptance_tests.md: BRT-S1, S2, X1..X8, plus S8)."""

import pytest
from strategy_fixtures import bar, frame, levels, mirror_frame, mirror_levels, reasons_at, run, zone

from tournament.strategies.brt import BRT


def base(obstacle=503.0):
    df = frame(price=499.0, vwap=499.0)
    zones = [zone(500.0, 500.0, ("PDH",), "pdh")]
    if obstacle is not None:
        zones.append(zone(obstacle, obstacle, ("PMH",), "pmh"))
    return df, levels(zones, pdh=500.0, pmh=obstacle)


def s1_bars(df):
    bar(df, 49, 499.95, 499.85, 499.90)
    bar(df, 50, 500.35, 499.95, 500.30)  # break
    bar(df, 51, 500.90, 500.55, 500.80)
    bar(df, 52, 501.00, 500.60, 500.90)
    bar(df, 53, 500.70, 500.20, 500.40)  # retest: RL 500.20, RH 500.70
    bar(df, 54, 500.90, 500.45, 500.80)  # trigger


def test_s1_valid_break_retest_hold():
    df, lv = base()
    s1_bars(df)
    c = run(BRT(), df, lv).candidates.iloc[0]
    assert c["trigger_slot"] == 54
    assert (c["entry_reference"], c["invalidation_level"], c["r_u"]) == pytest.approx(
        (500.80, 499.95, 0.85)
    )
    assert eval(c["target"].replace("null", "None"))["price"] == pytest.approx(503.00)


def test_s2_too_close_obstacle_is_not_skipped():
    df, lv = base(obstacle=501.50)
    s1_bars(df)
    r = run(BRT(), df, lv)
    assert r.candidates.empty and "RR_TOO_LOW" in reasons_at(r, 54)


def test_x1_runaway_wins_over_same_bar_retest():
    df, lv = base()
    s1_bars(df)
    bar(df, 51, 504.10, 500.30, 503.00)
    r = run(BRT(), df, lv)
    assert r.candidates.empty and "NO_RETEST" in reasons_at(r, 51)


def test_x2_new_rl_bar_cannot_trigger():
    df, lv = base()
    s1_bars(df)
    bar(df, 54, 500.65, 500.45, 500.60)  # no trigger
    bar(df, 55, 500.75, 500.10, 500.74)  # new RL; close above old RH 500.70 but RH is now 500.75
    bar(df, 56, 500.90, 500.50, 500.80)
    c = run(BRT(), df, lv).candidates.iloc[0]
    assert c["trigger_slot"] == 56 and c["invalidation_level"] == pytest.approx(500.10 - 0.25)


def test_x3_equal_lows_keep_the_earlier_bar():
    df, lv = base()
    s1_bars(df)
    bar(df, 54, 500.65, 500.45, 500.60)
    bar(df, 55, 500.90, 500.20, 500.60)  # equal low: RL bar stays 53 (RH 500.70)
    bar(df, 56, 500.90, 500.50, 500.75)
    c = run(BRT(), df, lv).candidates.iloc[0]
    assert c["trigger_slot"] == 56 and '"rh": 500.7' in c["features"]


def test_x4_retest_failed():
    df, lv = base()
    s1_bars(df)
    bar(df, 52, 500.60, 499.30, 499.40)
    r = run(BRT(), df, lv)
    assert r.candidates.empty and "RETEST_FAILED" in reasons_at(r, 52)


def test_x5_no_break_without_crossing():
    df, lv = base()
    for s in range(30, 60):
        bar(df, s, 500.45, 500.30, 500.40)  # already above E + brk_min from slot 30
    bar(df, 29, 500.45, 500.30, 500.40)
    r = run(BRT(), df, lv)
    assert all("BREAK" not in reasons_at(r, s) for s in range(31, 60))


def test_x6_fallback_target_without_obstacle():
    df, lv = base(obstacle=None)
    s1_bars(df)
    c = run(BRT(), df, lv).candidates.iloc[0]
    assert eval(c["target"].replace("null", "None"))["price"] == pytest.approx(500.80 + 2 * 0.85)


def test_x7_retest_wick_too_deep():
    df, lv = base()
    s1_bars(df)
    bar(df, 53, 500.70, 498.90, 500.10)
    r = run(BRT(), df, lv)
    assert r.candidates.empty and "RETEST_TOO_DEEP" in reasons_at(r, 53)


def test_x8_retest_within_both_limits():
    # No obstacle: with RL 499.05, r_u = 2.00, so the 503 obstacle would
    # correctly fail rr_min (2.2 < 2.4)
    df, lv = base(obstacle=None)
    s1_bars(df)
    bar(df, 53, 500.70, 499.05, 499.60)  # wick 0.95 A, close 0.4 A below E
    bar(df, 54, 500.90, 499.50, 500.80)
    r = run(BRT(), df, lv)
    assert "RETEST" in reasons_at(r, 53) and r.candidates.iloc[0]["trigger_slot"] == 54


def test_s8_bearish_mirror():
    df, lv = base()
    s1_bars(df)
    c = run(BRT(), mirror_frame(df), mirror_levels(lv)).candidates.iloc[0]
    assert c["underlying_direction"] == "bearish"
    assert (c["entry_reference"], c["invalidation_level"]) == pytest.approx(
        (1000 - 500.80, 1000 - 499.95)
    )
    assert eval(c["target"].replace("null", "None"))["price"] == pytest.approx(1000 - 503.00)


def test_hold_window_fixed_from_first_retest():
    df, lv = base()
    s1_bars(df)
    for s in range(54, 64):
        bar(df, s, 500.65, 500.30, 500.50)  # never above RH
    r = run(BRT(), df, lv)
    assert r.candidates.empty and "HOLD_TIMEOUT" in reasons_at(r, 63)
