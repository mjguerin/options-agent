"""The dashboard: loopback config, the data layer, the API client, and the app itself, run
headless with Streamlit's test harness."""

import json
import os

import httpx
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from streamlit.testing.v1 import AppTest

from tournament.alerts.config import load_alerts_config
from tournament.alerts.store import AlertStore
from tournament.api.app import create_app
from tournament.api.config import load_api_config
from tournament.api.store import ControlStore
from tournament.dashboard import backend as backend_module
from tournament.dashboard import data
from tournament.dashboard.backend import Backend
from tournament.dashboard.config import (
    DEFAULT_PATH,
    DashboardConfigError,
    load_dashboard_config,
)
from tournament.db import metadata
from tournament.orders.model import Intent, Purpose, Status
from tournament.orders.store import IntentStore, memory_engine
from tournament.secrets import ApiTokens

CFG = load_dashboard_config()
OWNER, VIEWER = "o" * 40, "v" * 40
T0 = pd.Timestamp("2026-10-26T15:00:00Z")
ACCT, CALL = "acct:vpc", "SPY261026C00765000"
APP = os.path.join(os.path.dirname(data.__file__), "app.py")


# ---- config ----


@pytest.mark.parametrize(
    "section, key, value",
    [("server", "host", "0.0.0.0"), ("api", "base_url", "http://10.0.0.5:8800")],
)
def test_the_dashboard_and_its_api_address_must_be_loopback(tmp_path, section, key, value):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw[section][key] = value
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(DashboardConfigError, match="loopback"):
        load_dashboard_config(p)
    assert CFG.server.host == "127.0.0.1" and CFG.api.base_url.startswith("http://127.0.0.1")


# ---- the data layer ----


def acct(**kw):
    detail = {"symbol": CALL, "qty": 13, "avg_entry": 1.44, "bid": 1.5, "unrealized_usd": 78.0}
    base = {
        "halted": None,
        "operator_halt": None,
        "entries_blocked": None,
        "positions": {CALL: 13},
        "positions_detail": [detail],
        "exposure_usd": 1872.0,
        "realized_pnl_usd": -20.0,
        "pending_orders": 0,
        "decisions": 3,
    }
    return {**base, **kw}


def test_account_status_names_the_most_serious_state_first():
    assert data.account_status(acct()) == "OK"
    assert data.account_status(acct(operator_halt="OPERATOR_PAUSE")) == "PAUSED"
    assert data.account_status(acct(operator_halt="OPERATOR_FLATTEN_ACCOUNT")) == "FLATTENED"
    assert data.account_status(acct(operator_halt="OPERATOR_EMERGENCY")) == "FLATTENED"
    assert (
        data.account_status(acct(halted="DAILY_LIMIT", operator_halt="OPERATOR_PAUSE")) == "HALTED"
    )
    assert data.account_status(acct(entries_blocked="mismatch", halted="DAILY_LIMIT")) == "BLOCKED"


def test_the_engine_light():
    ok = {"engine_reporting": True, "state": {"feeds": {"stock": {"connected": True}}}}
    assert data.engine_light(ok)[0] == "green"
    down = {
        "engine_reporting": True,
        "state": {"feeds": {"stock": {"connected": True}, "option": {"connected": False}}},
    }
    assert data.engine_light(down) == ("amber", "feed down: option")
    assert data.engine_light({"engine_reporting": False, "state": None})[0] == "red"


def test_the_accounts_table_adds_open_profit_to_the_published_numbers():
    t = data.accounts_table(
        {"accounts": {ACCT: acct(), "acct:orb": acct(positions={}, positions_detail=[])}}
    )
    row = t.set_index("account").loc[ACCT]
    assert (row["positions"], row["unrealized $"], row["realized $"], row["status"]) == (
        13,
        78.0,
        -20.0,
        "OK",
    )
    assert data.accounts_table(None).empty


def test_history_and_drawdown_from_the_published_series(tmp_path):
    f = tmp_path / "2026-10-26.jsonl"
    pnl = [(0, 0.0), (30, 100.0), (60, -50.0), (90, 20.0)]
    lines = []
    for sec, p in pnl:
        acc = {ACCT: {"realized_pnl_usd": p, "unrealized_usd": 0.0, "exposure_usd": 0}}
        lines.append(
            json.dumps({"at": (T0 + pd.Timedelta(seconds=sec)).isoformat(), "accounts": acc})
        )
    f.write_text("\n".join(lines))
    h = data.load_history(f)
    assert list(h[ACCT]) == [0.0, 100.0, -50.0, 20.0]
    dd = data.drawdown(h, 10_000.0)
    assert list(dd[ACCT]) == [0.0, 0.0, -150.0, -80.0]  # from the peak of 10,100
    assert data.load_history(tmp_path / "missing.jsonl").empty


def test_session_dirs_pick_the_latest_day_newest_run_first(tmp_path):
    for d in ("2026-10-23/shadow", "2026-10-26/shadow", "2026-10-26/shadow-r2"):
        (tmp_path / d).mkdir(parents=True)
    assert [p.name for p in data.session_dirs(tmp_path)] == ["shadow-r2", "shadow"]
    assert [p.name for p in data.session_dirs(tmp_path, "2026-10-23")] == ["shadow"]
    assert data.session_dirs(tmp_path / "nothing") == []


def make_orders_db(path):
    engine = create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    store = IntentStore(engine)
    i = Intent(
        "opt-vpc-x-e0",
        ACCT,
        "c1",
        "vpc",
        Purpose.ENTRY,
        "buy",
        CALL,
        13,
        "limit",
        1.44,
        "day",
        Status.PENDING_SUBMIT,
        T0,
    )
    store.insert(i)
    store.event(i.client_order_id, T0, "submit")
    store.save(i.with_(status=Status.FILLED, qty_filled=13, avg_fill_price=1.44))
    store.event(
        i.client_order_id, T0 + pd.Timedelta(seconds=1.5), "status", status="FILLED", filled=13
    )


def test_orders_are_read_back_and_fill_latency_is_measured(tmp_path):
    db = tmp_path / "accounts.sqlite"
    make_orders_db(db)
    orders, events = data.read_orders(db)
    assert orders.loc[0, "status"] == "FILLED" and len(events) == 2
    assert data.fill_latencies(orders, events).loc[0, "latency_s"] == pytest.approx(1.5)
    assert data.read_orders(tmp_path / "none.sqlite")[0].empty


def test_ops_reports_newest_first(tmp_path):
    for d in ("2026-10-23", "2026-10-26"):
        (tmp_path / f"nightly_{d}.json").write_text(json.dumps({"date": d, "missing": {}}))
    assert [r["date"] for r in data.ops_reports(tmp_path)] == ["2026-10-26", "2026-10-23"]


# ---- the API client ----


class Env:
    pass


@pytest.fixture()
def world(tmp_path):
    engine = memory_engine()
    store = ControlStore(engine)
    state_file = tmp_path / "data" / "run" / "engine.state.json"
    state_file.parent.mkdir(parents=True)
    now = pd.Timestamp.now(tz="UTC")
    decision = {
        "at": T0.isoformat(),
        "candidate_id": "c1",
        "accepted": False,
        "reasons": ["SPREAD_TOO_WIDE_ABS"],
        "contract": CALL,
        "quantity": None,
    }
    strategy = {
        "version": "vpc-1",
        "config_hash": "abc123",
        "slot": 42,
        "outcome": "WATCHING",
        "setup_state": "WATCHING_PULLBACK",
        "readiness": "READY",
        "blocks": [],
        "reasons": [],
    }
    state = {
        "at": now.isoformat(),
        "session": "2026-10-26",
        "mode": "shadow",
        "slot": 42,
        "feeds": {
            "stock": {"connected": True},
            "option": {"connected": True, "lag_s": {"p50": 0.007, "p99": 0.016}},
        },
        "accounts": {
            ACCT: acct(recent_decisions=[decision]),
            "acct:orb": acct(positions={}, positions_detail=[], exposure_usd=0.0),
        },
        "strategies": {"vpc": strategy},
    }
    state_file.write_text(json.dumps(state))
    alerts = AlertStore(engine, load_alerts_config())
    app = create_app(store, load_api_config(), ApiTokens(OWNER, VIEWER), state_file, alerts)
    http = TestClient(app, base_url="http://127.0.0.1:8800")
    w = Env()
    w.store, w.alerts, w.state_file, w.http, w.root = store, alerts, state_file, http, tmp_path
    w.client = data.ApiClient(CFG.api.base_url, 5, VIEWER, OWNER, http=http)
    return w


def test_the_client_reads_with_the_viewer_token_and_controls_with_the_owner_token(world):
    assert world.client.status()["engine_reporting"] is True
    r = world.client.control({"command": "PAUSE_ENTRIES", "scope": "global"})
    assert r["status"] == "PENDING"
    only_viewer = data.ApiClient(CFG.api.base_url, 5, VIEWER, VIEWER, http=world.http)
    with pytest.raises(data.ApiError) as e:
        only_viewer.control({"command": "PAUSE_ENTRIES", "scope": "global"})
    assert e.value.status == 403
    assert world.client.audit(5)[0]["outcome"] == "DENIED"


def test_an_unreachable_api_is_an_error_the_page_can_show():
    class Down:
        def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    c = data.ApiClient(CFG.api.base_url, 5, VIEWER, OWNER, http=Down())
    with pytest.raises(data.ApiError) as e:
        c.status()
    assert e.value.status == 0 and "unreachable" in e.value.detail


# ---- the app, headless ----


@pytest.fixture()
def app(world, monkeypatch):
    make_orders_db(
        (world.root / "data" / "live" / "2026-10-26" / "shadow").mkdir(parents=True)
        or (world.root / "data" / "live" / "2026-10-26" / "shadow" / "accounts.sqlite")
    )
    monkeypatch.setattr(
        backend_module, "override", Backend(CFG, world.client, None, root=world.root)
    )
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    return at, world


def switch(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception
    return at


def test_the_header_shows_the_engine_and_the_session(app):
    at, _ = app
    labels = {m.label: m.value for m in at.metric}
    assert (
        "reporting" in labels["Engine"]
        and labels["Session"] == "2026-10-26"
        and labels["Slot"] == "42"
    )


def test_overview_lists_the_accounts(app):
    at, _ = app
    df = at.dataframe[0].value
    assert (
        set(df["account"]) == {ACCT, "acct:orb"}
        and df.set_index("account").loc[ACCT, "status"] == "OK"
    )


def test_strategies_page_shows_state_decisions_and_exits(app):
    at, _ = app
    switch(at, "Strategies")
    assert any(m.value == "WATCHING_PULLBACK" for m in at.metric)
    decisions = [d.value for d in at.dataframe if "reasons" in d.value.columns]
    assert decisions and "SPREAD_TOO_WIDE_ABS" in decisions[0]["reasons"].iloc[0]
    assert any("premium stop at 20%" in c.value for c in at.caption)


def test_execution_page_shows_orders_and_fill_time(app):
    at, _ = app
    switch(at, "Execution")
    assert {m.label for m in at.metric} >= {"Orders", "Filled", "Median fill time"}
    assert any(m.label == "Orders" and m.value == "1" for m in at.metric)


def test_operations_page_shows_incidents_and_the_audit_log(app):
    at, world = app
    world.alerts.open(T0, "STOCK_FEED_DOWN", "engine", "not connected")
    world.client.control({"command": "PAUSE_ENTRIES", "scope": "global"})
    switch(at, "Operations")
    texts = " ".join(str(d.value.to_dict("records")) for d in at.dataframe)
    assert "STOCK_FEED_DOWN" in texts and "PAUSE_ENTRIES" in texts


def test_a_confirmed_control_needs_the_typed_phrase_and_then_queues_one_command(app):
    at, world = app
    switch(at, "Controls")
    at.selectbox[0].set_value("FLATTEN_ACCOUNT").run()
    [s for s in at.selectbox if s.label == "Account"][0].set_value(ACCT).run()
    prepare = [b for b in at.button if b.label == "Prepare FLATTEN_ACCOUNT"][0]
    prepare.click().run()
    assert not at.exception
    assert world.store.pending(pd.Timestamp.now(tz="UTC")) == []  # preparing queues nothing
    confirm = [b for b in at.button if b.label == "Confirm and send"][0]
    assert confirm.disabled
    at.text_input[0].set_value("FLATTEN_ACCOUNT wrong").run()
    assert [b for b in at.button if b.label == "Confirm and send"][0].disabled
    at.text_input[0].set_value(f"FLATTEN_ACCOUNT {ACCT}").run()
    [b for b in at.button if b.label == "Confirm and send"][0].click().run()
    assert not at.exception
    (cmd,) = world.store.pending(pd.Timestamp.now(tz="UTC"))
    assert (cmd.command, cmd.account) == ("FLATTEN_ACCOUNT", ACCT)


def test_an_unconfirmed_control_is_one_click(app):
    at, world = app
    switch(at, "Controls")
    scope = [r for r in at.radio if r.label == "Scope"][0]
    scope.set_value("global").run()
    [b for b in at.button if b.label == "Send PAUSE_ENTRIES"][0].click().run()
    assert [c.command for c in world.store.pending(pd.Timestamp.now(tz="UTC"))] == ["PAUSE_ENTRIES"]


def test_missing_tokens_show_the_setup_step_instead_of_crashing(world, monkeypatch):
    monkeypatch.setattr(backend_module, "override", Backend(CFG, None, "No .env", root=world.root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and any("Dashboard setup" in e.value for e in at.error)
    assert any("openssl rand" in c.value for c in at.code)


def test_an_unreachable_api_leaves_the_page_up_with_a_warning(world, monkeypatch):
    class Down:
        def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    client = data.ApiClient(CFG.api.base_url, 5, VIEWER, OWNER, http=Down())
    monkeypatch.setattr(backend_module, "override", Backend(CFG, client, None, root=world.root))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and any("API" in w.value for w in at.warning)


def write_ledger(root, day, account, net, run="shadow", ok=True):
    d = root / "data" / "live" / day / run / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "summary": {
            "session_date": day, "account": account, "start_equity_usd": 10_000.0, "trades": 2,
            "net_realized_eval_usd": net, "open_estimate_usd": 0.0, "net_pnl_est_usd": net,
            "fees_usd": 1.3, "stress_net_usd": {"base": net, "plus_2_ticks": net - 40},
            "operational": {"uncertain_slots": 2, "incidents": {"STOCK_FEED_DOWN": 1}},
        },
        "metrics": {"closed_trades": 2, "win_rate": 0.5, "max_drawdown_usd": -25.0},
        "reconciliation": {"ok": ok, "checks": {"cash": {"ok": ok}}},
        "trades": [],
    }  # fmt: skip
    (d / f"{account.replace(':', '_')}.json").write_text(json.dumps(body))
    rank = [{"rank": 1, "account": account, "net_pnl_est_usd": net, "promotion_qualified": net > 0,
             "leader_not_qualified": net <= 0}]  # fmt: skip
    (d / "ranking.json").write_text(json.dumps(rank))


def test_the_ledger_data_keeps_the_newest_run_and_sums_the_days(tmp_path):
    write_ledger(tmp_path, "2026-10-26", ACCT, 100.0)
    write_ledger(tmp_path, "2026-10-26", ACCT, 120.0, run="shadow-r2")  # the restart's record
    write_ledger(tmp_path, "2026-10-27", ACCT, -30.0)
    write_ledger(tmp_path, "2026-10-27", "acct:orb", 50.0, ok=False)
    got = data.load_ledgers(tmp_path / "data" / "live")
    assert len(got) == 3  # one per session day and account, the newest run of each
    t = data.cumulative(got).set_index("account")
    assert t.loc[ACCT, "net P&L est $"] == 90.0 and t.loc[ACCT, "sessions"] == 2
    assert t.loc[ACCT, "uncertain slots"] == 4 and t.loc[ACCT, "incidents"] == 2  # outages visible
    assert bool(t.loc["acct:orb", "books reconcile"]) is False
    assert data.cumulative([]).empty and data.load_ledgers(tmp_path / "none") == []


def test_research_page_shows_the_ledgers_and_an_unprofitable_leader(app):
    at, world = app
    write_ledger(world.root, "2026-10-26", ACCT, -10.0)
    switch(at, "Research")
    tables = [d.value for d in at.dataframe]
    assert any("net P&L est $" in t.columns for t in tables)
    ranking = [t for t in tables if "promotion qualified" in t.columns][0]
    assert ranking.iloc[0]["note"] == "leads, but does not qualify"


def test_research_page_without_ledgers_says_so(app):
    at, _ = app
    switch(at, "Research")
    assert any("None yet" in i.value for i in at.info)
