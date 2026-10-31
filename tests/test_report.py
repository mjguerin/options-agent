"""The one-page report: config, what it collects from a session folder, the page, the server."""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from tournament.report.config import DEFAULT_PATH, ReportConfigError, load_report_config
from tournament.report.data import NOT_RECORDED, collect, session_folders
from tournament.report.render import page
from tournament.report.server import create_app

CFG = load_report_config()
NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
DAY = "2026-10-29"


def edited(tmp_path, **sections):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    for k, v in sections.items():
        raw[k].update(v)
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def trade(cid, net, sym="SPY261029P00769000"):
    return {
        "candidate_id": cid, "strategy_id": "brt", "symbol": sym, "right": "P", "qty": 10,
        "entry_at": "2026-10-29T14:00:02+00:00", "exit_at": "2026-10-29T14:05:00+00:00",
        "broker_entry": 1.50, "broker_exit": 1.20, "net_eval_usd": net, "holding_s": 298.0,
        "exit_reasons": ["PREMIUM_STOP"], "open_qty": 0,
    }  # fmt: skip


def make_day(root, day=DAY, run="shadow", decisions=True, summary_outcomes=None):
    run_dir = root / "data" / "live" / day / run
    (run_dir / "ledger").mkdir(parents=True)
    body = {
        "summary": {"account": "acct:brt", "net_pnl_est_usd": -300.0},
        "trades": [trade("c1", -300.0)],
    }
    (run_dir / "ledger" / "acct_brt.json").write_text(json.dumps(body))
    (run_dir / "ledger" / "paths.json").write_text(
        json.dumps({"acct:brt": [{"candidate_id": "c1", "option": {
            "hold": {"peak_bid": 1.6, "low_bid": 1.2}, "after_exit": {"peak_bid": 1.4},
            "session_end": {"last_bid": 0.1}}}]})
    )  # fmt: skip
    rec = run_dir / "records" / "live-run"
    rec.mkdir(parents=True)
    t0 = pd.Timestamp("2026-10-29T14:00:00Z")
    cands = pd.DataFrame(
        [
            {"candidate_id": cid, "account_binding": "acct:brt", "strategy_id": "brt",
             "underlying_direction": "bearish", "signal_bar_end": t0 + pd.Timedelta(hours=h),
             "entry_reference": 768.0, "invalidation_level": 769.0,
             "target": json.dumps({"price": 766.0}), "submission_eligible": True}
            for cid, h in (("c1", 0), ("c2", 2))
        ]
    )  # fmt: skip
    cands.to_parquet(rec / "candidates.parquet")
    pd.DataFrame(
        [
            {"candidate_id": cid, "underlying_direction": "bearish",
             "reference_entry_price": 768.0, "r_u": 1.0, "exit_primary_reason": "INVALIDATION",
             "reference_exit_price": 769.0, "reference_exit_time": t0 + pd.Timedelta(hours=h + 1),
             "exit_slot": 20, "entry_slot": 10}
            for cid, h in (("c1", 0), ("c2", 2))
        ]
    ).to_parquet(rec / "reference_positions.parquet")  # fmt: skip
    pd.DataFrame(
        {"slot": range(8, 24), "high": [768.4] * 16, "low": [767.2] * 16, "close": [768.0] * 16}
    ).to_parquet(run_dir / "bars.parquet")
    pd.DataFrame(
        [
            {"strategy_id": "brt", "slot": 5, "from_state": "IDLE", "to_state": "BROKEN",
             "reason_codes": ["BREAK"]},
            {"strategy_id": "brt", "slot": 7, "from_state": "BROKEN", "to_state": "IDLE",
             "reason_codes": ["RETEST_FAILED"]},
            {"strategy_id": "brt", "slot": 10, "from_state": "IDLE", "to_state": "IN_POSITION",
             "reason_codes": ["BRT_BEARISH_HOLD"]},
        ]
    ).to_parquet(rec / "transitions.parquet")  # fmt: skip
    (run_dir / "summary.json").write_text(
        json.dumps({"accounts": {"accounts": {"acct:brt": {"outcomes": summary_outcomes or {}}}}})
    )
    if decisions:
        line = {"candidate_id": "c2", "accepted": False, "reasons": ["DAILY_LIMIT"],
                "contract": None, "quantity": None, "requested_quantity": None}  # fmt: skip
        (root / "data" / "live" / day / "decisions.jsonl").write_text(json.dumps(line) + "\n")
    return run_dir


@pytest.fixture
def root(tmp_path):
    make_day(tmp_path)
    return tmp_path


def brt(data):
    return next(a for a in data["accounts"] if a["key"] == "brt")


# ---- config -----------------------------------------------------------------------------------


def test_the_shipped_config_loads_and_hashes_stably():
    assert CFG.config_hash == load_report_config().config_hash and len(CFG.config_hash) == 64
    assert [a.id for a in CFG.accounts] == ["vpc", "orb", "brt", "fbr", "vmr"]


def test_the_report_is_loopback_only(tmp_path):
    with pytest.raises(ReportConfigError):
        load_report_config(edited(tmp_path, server={"host": "0.0.0.0"}))


# ---- what it collects ---------------------------------------------------------------------------


def test_trades_signals_and_the_reasons_a_signal_was_not_traded(root):
    a = brt(collect(CFG, root, NOW))
    (t,) = a["trades"]
    assert (t["net_usd"], t["peak_bid"], t["low_bid"], t["after_exit_peak"]) == (
        -300.0,
        1.6,
        1.2,
        1.4,
    )
    assert t["ref"]["result_r"] == pytest.approx(-1.0)  # the strategy's own result, in R
    done, skipped = a["signals"]
    assert (done["status"], done["contract"], done["qty"]) == ("EXECUTED", "SPY261029P00769000", 10)
    assert (skipped["status"], skipped["reasons"]) == ("REJECTED", ["DAILY_LIMIT"])
    assert skipped["ref"]["result_r"] == pytest.approx(-1.0)  # a signal not traded still has one
    assert skipped["target"] == 766.0 and skipped["entry_reference"] == 768.0


def test_a_day_without_saved_decisions_is_explained_only_when_it_can_be(tmp_path):
    make_day(tmp_path, decisions=False, summary_outcomes={"ACCEPTED": 1, "STALE_QUOTE": 1})
    skipped = brt(collect(CFG, tmp_path, NOW))["signals"][1]
    assert skipped["reasons"] == ["STALE_QUOTE"]  # the account's one unexecuted signal
    other = tmp_path / "again"
    make_day(other, decisions=False)  # no tally either: it is said plainly
    assert brt(collect(CFG, other, NOW))["signals"][1]["reasons"] == [NOT_RECORDED]


def test_the_setup_funnel_and_the_daily_series(root):
    a = brt(collect(CFG, root, NOW))
    assert a["funnel"][DAY] == {"started": 1, "ended": {"RETEST_FAILED": 1}, "signals": 2}
    assert a["daily"] == [{"day": DAY, "net_usd": -300.0}]


def test_the_newest_run_of_a_day_is_the_one_shown(tmp_path):
    for run in ("shadow", "shadow-r2", "shadow-r10"):
        (tmp_path / "data" / "live" / DAY / run / "ledger").mkdir(parents=True)
    assert session_folders(tmp_path / "data" / "live")[DAY].name == "shadow-r10"


def test_accounts_without_activity_still_have_a_card(root):
    data = collect(CFG, root, NOW)
    assert [a["key"] for a in data["accounts"]] == ["vpc", "orb", "brt", "fbr", "vmr"]
    assert data["accounts"][0]["trades"] == [] and data["days"] == [DAY]


def test_live_positions_show_only_while_the_engine_is_reporting(tmp_path, root):
    state = {
        "at": NOW.isoformat(), "session": DAY, "mode": "shadow", "slot": 40, "feeds": {},
        "accounts": {"acct:brt": {"positions_detail": [{"symbol": "X", "qty": 3}], "halted": None,
                                  "pending_orders": 0, "exposure_usd": 400, "decisions": 1}},
    }  # fmt: skip
    run = root / "data" / "run"
    run.mkdir(parents=True)
    (run / "engine.state.json").write_text(json.dumps(state))
    fresh = collect(CFG, root, NOW)
    assert fresh["engine"]["reporting"] is True and brt(fresh)["live"]["positions"][0]["qty"] == 3
    stale = collect(CFG, root, NOW + pd.Timedelta(minutes=5))
    assert stale["engine"]["reporting"] is False  # the page then says it is not live


def test_nan_and_timestamps_become_plain_json(root):
    json.dumps(collect(CFG, root, NOW), allow_nan=False)  # raises if anything is left over


# ---- the page and the server --------------------------------------------------------------------


def test_the_page_embeds_the_data_and_cannot_be_broken_by_text_in_it(root):
    data = collect(CFG, root, NOW)
    data["accounts"][0]["name"] = "</script><script>alert(1)</script>"
    html = page(data, poll=True)
    assert "__DATA__" not in html and "__POLL__" not in html and "const POLL = true" in html
    assert html.count("</script>") == 2  # the data tag's and the code's, nothing injected
    assert "const POLL = false" in page(data, poll=False)


def test_the_page_opens_on_the_summary_and_has_a_menu_to_pick_one_account(root):
    html = page(collect(CFG, root, NOW), poll=False)
    assert "view: 'all'" in html  # the opening view is the all-accounts summary
    assert "function summaryTable" in html and 'class="pick"' in html and "function setView" in html


def test_the_server_serves_the_page_the_data_and_a_health_check(root):
    client = TestClient(create_app(CFG, root))
    assert "Options Tournament" in client.get("/").text
    body = client.get("/data.json").json()
    assert body["days"] == [DAY] and body["limits"]["trades"] == CFG.tables.trades_max_rows
    assert client.get("/health").json()["ok"] is True
    assert client.get("/docs").status_code == 404  # no interactive API page on this server


def test_a_decision_is_recorded_for_every_candidate_and_a_failing_record_blocks_nothing():
    from test_account_runtime import at as t_at
    from test_account_runtime import build, cand, prices

    rt, sim, m = build()
    seen = []
    rt.record = lambda d, c: seen.append((d.candidate_id, d.accepted, c["candidate_id"]))
    prices(m, t_at(0))
    assert rt.on_candidate(t_at(0), cand()).accepted
    assert seen == [("c1", True, "c1")]
    rt.record = lambda d, c: 1 / 0
    d = rt.on_candidate(t_at(1), cand("c2", expires_at=t_at(100)))
    assert d.candidate_id == "c2"  # the decision stands though the record could not be written


def test_the_accounts_write_one_json_line_per_decision(tmp_path):
    from tournament.accounts.runtime import EntryDecision
    from tournament.live.shadow_accounts import ShadowAccounts

    acc = ShadowAccounts.__new__(ShadowAccounts)
    acc.decisions_file = tmp_path / "decisions.jsonl"
    d = EntryDecision("c9", False, ("STALE_QUOTE", "DELAYED_DELIVERY"), at=pd.Timestamp(NOW))
    acc._record_decision(d, {"account_binding": "acct:brt"})
    (row,) = [json.loads(x) for x in acc.decisions_file.read_text().splitlines()]
    assert row["account"] == "acct:brt" and row["accepted"] is False
    assert row["reasons"] == ["STALE_QUOTE", "DELAYED_DELIVERY"] and row["contract"] is None
