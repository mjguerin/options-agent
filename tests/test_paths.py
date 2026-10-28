"""Where each trade went: the option's path and the strategy's own outcome (D-80, D-81)."""

import json

import pandas as pd
import pytest

from tournament.ledger.config import load_ledger_config
from tournament.ledger.paths import build_paths

CFG = load_ledger_config()
SYM = "SPY261027P00767000"
T0 = pd.Timestamp("2026-10-27T14:38:00Z")


def make_session(tmp_path, with_quotes=True, with_ref=True):
    (tmp_path / "ledger").mkdir()
    trade = {
        "candidate_id": "c1", "symbol": SYM, "qty": 10, "broker_entry": 1.00, "broker_exit": 0.80,
        "entry_at": (T0 + pd.Timedelta(seconds=3)).isoformat(),
        "exit_at": (T0 + pd.Timedelta(seconds=123)).isoformat(), "exit_reasons": ["PREMIUM_STOP"],
    }  # fmt: skip
    body = {"summary": {"account": "acct:brt"}, "trades": [trade]}
    (tmp_path / "ledger" / "acct_brt.json").write_text(json.dumps(body))
    if with_quotes:
        secs = pd.date_range(T0, periods=3600, freq="1s")
        bid = [0.0] * len(secs)  # a zero bid is no market and must be ignored
        shape = {0: 0.0, 3: 1.00, 30: 1.10, 100: 0.95, 123: 0.80, 200: 0.90, 400: 0.50}
        last = 0.0
        for i in range(len(secs)):
            last = shape.get(i, last)
            bid[i] = last
        bid[3600 - 1] = 0.02
        pd.DataFrame({"second": secs, "symbol": SYM, "bid": bid, "ask": [b + 0.01 for b in bid]}
                     ).to_parquet(tmp_path / "option_quotes_1s.parquet")  # fmt: skip
    if with_ref:
        bars = pd.DataFrame(
            {
                "slot": range(68, 80),
                "high": [767.5] * 12,
                "low": [766.9] * 12,
                "close": [767.0] * 12,
            }
        )
        bars.loc[bars.slot == 72, "low"] = 766.0  # the best the underlying did for a bearish trade
        bars.loc[bars.slot == 75, "high"] = 768.0  # and the worst against it
        bars.to_parquet(tmp_path / "bars.parquet")
        d = tmp_path / "records" / "run-1"
        d.mkdir(parents=True)
        pd.DataFrame(
            [{"candidate_id": "c1", "underlying_direction": "bearish",
              "reference_entry_price": 767.0, "r_u": 1.0, "exit_primary_reason": "FOLLOW_THROUGH",
              "reference_exit_price": 767.8, "reference_exit_time": T0 + pd.Timedelta(minutes=10),
              "exit_slot": 78, "entry_slot": 68}]
        ).to_parquet(d / "reference_positions.parquet")  # fmt: skip
    return tmp_path


def test_the_option_path_gives_the_peak_the_low_the_aftermath_and_the_held_to_end_result(tmp_path):
    build_paths(make_session(tmp_path), CFG)
    (row,) = json.loads((tmp_path / "ledger" / "paths.json").read_text())["acct:brt"]
    o = row["option"]
    assert o["hold"]["peak_bid"] == 1.10 and o["hold"]["peak_after_s"] == pytest.approx(27)
    assert o["hold"]["pnl_at_peak_usd"] == pytest.approx(100.0)
    assert o["hold"]["low_bid"] == 0.80 and o["hold"]["low_vs_entry_pct"] == pytest.approx(-0.20)
    assert o["exit_pnl_usd"] == pytest.approx(-200.0)
    assert o["after_exit"]["peak_bid"] == 0.90 and o["after_exit"]["window_min"] == 30.0
    assert o["session_end"]["last_bid"] == 0.02
    assert o["session_end"]["held_to_end_pnl_usd"] == pytest.approx(-980.0)
    assert o["exit_vs_held_to_end_usd"] == pytest.approx(780.0)  # what the stop saved


def test_the_reference_outcome_is_in_units_of_its_own_risk(tmp_path):
    build_paths(make_session(tmp_path), CFG)
    ref = json.loads((tmp_path / "ledger" / "paths.json").read_text())["acct:brt"][0]["reference"]
    assert ref["exit_reason"] == "FOLLOW_THROUGH"
    assert ref["result_r"] == pytest.approx(-0.8)  # bearish: entered 767.0, left 767.8, risk 1.0
    assert ref["best_excursion_r"] == pytest.approx(1.0)  # ran to 766.0, one R for the trade
    assert ref["worst_excursion_r"] == pytest.approx(-1.0)  # and to 768.0 against it


def test_missing_files_leave_the_review_partial_never_wrong(tmp_path):
    build_paths(make_session(tmp_path, with_quotes=False, with_ref=False), CFG)
    (row,) = json.loads((tmp_path / "ledger" / "paths.json").read_text())["acct:brt"]
    assert row["option"] == {"available": False} and row["reference"] is None
    assert row["entry"] == 1.00 and row["exit"] == 0.80  # the ledger's own figures are still there
