"""Counterfactual exit rules on recorded paths (access log #25): hand-computed small paths."""

import json

import pandas as pd
import pytest

from tournament.ledger.config import load_ledger_config
from tournament.ledger.exit_rules import (
    AS_EXECUTED,
    AS_SPECIFIED,
    option_rule_names,
    option_rules,
    r_rule_names,
    r_rules,
)
from tournament.ledger.paths import build_paths

CFG = load_ledger_config()
RULES = CFG.paths.exit_rules  # fill delay 1 s, breakeven after 5/10/15%, half at 10/20%, trail
T0 = pd.Timestamp("2026-10-28T15:00:00Z")


def path(bids: list[float]) -> pd.Series:
    return pd.Series(bids, index=[T0 + pd.Timedelta(seconds=i) for i in range(len(bids))])


def run(bids, exit_px, entry=1.00, qty=10):
    p = path(bids)
    return option_rules(p, entry, exit_px, qty, 100, RULES, p.index[-1])


def test_the_names_follow_the_config():
    assert option_rule_names(RULES)[0] == AS_EXECUTED and len(option_rule_names(RULES)) == 7
    assert r_rule_names(RULES)[0] == AS_SPECIFIED and len(r_rule_names(RULES)) == 5


def test_a_breakeven_stop_fills_one_second_after_it_fires():
    # +6% at second 1 arms it; the bid is back at entry at second 3; it sells at second 4's bid
    r = run([1.00, 1.06, 1.02, 1.00, 0.99, 0.80], exit_px=0.80)
    assert r[AS_EXECUTED] == pytest.approx(-200.0)
    assert r["Breakeven stop after +5%"] == pytest.approx((0.99 - 1.00) * 1000)  # -10, not -200
    assert r["Breakeven stop after +10%"] == pytest.approx(-200.0)  # never armed: as executed


def test_selling_half_keeps_the_rest_on_the_real_exit():
    r = run([1.00, 1.05, 1.12, 1.13, 1.10, 0.90], exit_px=0.90)
    # +10% reached at second 2; fills at second 3's bid, 1.13; the other half exits at 0.90
    assert r["Sell half at +10%"] == pytest.approx(0.5 * 1000 * 0.13 + 0.5 * 1000 * -0.10)
    assert r["Sell half at +20%"] == pytest.approx(-100.0)  # never reached: as executed


def test_a_trailing_stop_sells_after_a_give_back_from_the_peak():
    r = run([1.00, 1.10, 1.30, 1.25, 1.16, 1.15, 0.90], exit_px=0.90)
    name = "Trail 10% after +10%"
    # armed at 1.10, peak 1.30; 1.16 is 10.8% under the peak at second 4; fills at second 5: 1.15
    assert r[name] == pytest.approx((1.15 - 1.00) * 1000)


def test_a_rule_that_would_fire_after_the_real_exit_never_acts():
    p = path([1.00, 1.06, 1.00])  # the position was sold at second 2; the fill would be second 3
    r = option_rules(p, 1.00, 1.00, 10, 100, RULES, p.index[-1])
    assert r["Breakeven stop after +5%"] == r[AS_EXECUTED] == 0.0


def ref(direction="bullish", entry=100.0, ru=1.0, exit_price=99.0, entry_slot=10, exit_slot=14):
    return pd.Series(
        {"underlying_direction": direction, "reference_entry_price": entry, "r_u": ru,
         "reference_exit_price": exit_price, "entry_slot": entry_slot, "exit_slot": exit_slot}
    )  # fmt: skip


def bars(closes):
    return pd.DataFrame({"slot": range(11, 11 + len(closes)), "close": closes})


def test_r_rules_lock_half_at_the_tier_and_can_stop_the_rest_at_entry():
    # closes in R: +0.6, +0.2, -0.3, then the strategy exits at -1R on the last bar
    r = r_rules(ref(), bars([100.6, 100.2, 99.7, 99.0]), RULES)
    assert r[AS_SPECIFIED] == pytest.approx(-1.0)
    assert r["Sell half at +0.5R"] == pytest.approx(0.5 * 0.6 + 0.5 * -1.0)
    # the rest is stopped the first time a close is at or below entry: -0.3
    assert r["Sell half at +0.5R, rest stopped at entry"] == pytest.approx(0.5 * 0.6 + 0.5 * -0.3)
    assert r["Sell half at +1R"] == pytest.approx(-1.0)  # +1R never closed: as specified


def test_r_rules_handle_a_bearish_position_and_an_unclosed_one():
    r = r_rules(ref("bearish", exit_price=101.0), bars([99.4, 99.9, 101.0]), RULES)
    assert r[AS_SPECIFIED] == pytest.approx(-1.0) and r["Sell half at +0.5R"] == pytest.approx(-0.2)
    open_ref = ref()
    open_ref["reference_exit_price"] = float("nan")
    assert r_rules(open_ref, bars([100.6]), RULES) is None


def test_the_close_writes_each_trades_rule_results_beside_its_path(tmp_path):
    (tmp_path / "ledger").mkdir()
    sym = "SPY261028C00770000"
    trade = {
        "candidate_id": "c1", "symbol": sym, "qty": 10, "broker_entry": 1.00, "broker_exit": 0.80,
        "entry_at": T0.isoformat(), "exit_at": (T0 + pd.Timedelta(seconds=5)).isoformat(),
        "exit_reasons": ["PREMIUM_STOP"],
    }  # fmt: skip
    body = {"summary": {"account": "acct:brt"}, "trades": [trade]}
    (tmp_path / "ledger" / "acct_brt.json").write_text(json.dumps(body))
    bids = [1.00, 1.06, 1.02, 1.00, 0.99, 0.80]
    pd.DataFrame(
        {"second": [T0 + pd.Timedelta(seconds=i) for i in range(6)], "symbol": sym, "bid": bids,
         "ask": [b + 0.01 for b in bids]}
    ).to_parquet(tmp_path / "option_quotes_1s.parquet")  # fmt: skip
    build_paths(tmp_path, CFG)
    (row,) = json.loads((tmp_path / "ledger" / "paths.json").read_text())["acct:brt"]
    assert row["rules"][AS_EXECUTED] == pytest.approx(-200.0)
    assert row["rules"]["Breakeven stop after +5%"] == pytest.approx(-10.0)
