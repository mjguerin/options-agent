"""The end-of-session ledger writer: real account runtimes, real fills, both ledgers on disk."""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from test_account_runtime import CALL, cand, prices
from test_account_runtime import at as t_at
from test_recovery import build_durable

from tournament.ledger.config import load_ledger_config
from tournament.ledger.store import latest_sessions
from tournament.live.ledger_writer import incident_counts, write_session_ledgers
from tournament.live.shadow_accounts import ShadowAccounts
from tournament.orders.store import memory_engine

CFG = load_ledger_config()


def traded(tmp_path):
    """A full round trip: 13 bought at the 1.44 ask, sold at the 1.50 bid after a target exit."""
    rt, sim, m = build_durable(tmp_path)
    prices(m, t_at(0))
    assert rt.on_candidate(t_at(0), cand()).accepted
    for s in (1.5, 2.5):
        prices(m, t_at(s))
        rt.on_tick(t_at(s))
    prices(m, t_at(20), call=(1.50, 1.51))
    rt.on_reference_exit(t_at(20), "c1", "TARGET")
    for s in (21.5, 22.5):
        prices(m, t_at(s), call=(1.50, 1.51))
        rt.on_tick(t_at(s))
    assert rt.orders.position(CALL) == 0
    acc = ShadowAccounts.__new__(ShadowAccounts)
    acc.router = SimpleNamespace(runtimes={rt.binding: rt})
    acc.brokers = {rt.binding: sim}
    session = SimpleNamespace(
        day=SimpleNamespace(session_date=date(2026, 10, 26)),
        bars=[
            {"interval_status": "PRESENT"},
            {"interval_status": "DATA_MISSING_OR_UNCERTAIN"},
            {"interval_status": "HALTED"},
        ],
    )
    return rt, acc, session


def test_both_ledgers_are_written_and_reconcile(tmp_path):
    rt, acc, session = traded(tmp_path)
    engine = memory_engine()
    out = tmp_path / "ledger"
    res = write_session_ledgers(
        acc, session, CFG, t_at(30), "exp-1", "shadow", out, tmp_path / "none.jsonl", None, engine
    )
    body = json.loads((out / "acct_vpc.json").read_text())
    s = body["summary"]
    assert body["reconciliation"]["ok"] and res["accounts"][rt.binding]["reconciled"]
    assert s["broker_realized_gross_usd"] == pytest.approx((1.50 - 1.44) * 13 * 100)
    assert s["eval_realized_gross_usd"] == pytest.approx(78.0)  # same quotes: the ledgers agree
    assert s["net_pnl_est_usd"] == pytest.approx(78.0) and s["open_positions"] == []
    assert body["trades"][0]["exit_reasons"] == ["REF_TARGET"]
    assert s["operational"]["uncertain_slots"] == 1 and s["operational"]["halted_slots"] == 1
    assert [r["account"] for r in json.loads((out / "ranking.json").read_text())] == [rt.binding]
    (row,) = latest_sessions(engine, "exp-1", "shadow")
    assert (
        row["revision"] == 1
        and row["reconciled"] is True
        and res["accounts"][rt.binding]["stored"]["new"]
    )
    again = write_session_ledgers(
        acc, session, CFG, t_at(31), "exp-1", "shadow", out, tmp_path / "none.jsonl", None, engine
    )
    assert again["accounts"][rt.binding]["stored"] == {"revision": 1, "new": False}


def test_the_ledger_is_still_written_to_disk_when_the_database_is_down(tmp_path):
    rt, acc, session = traded(tmp_path)

    class Down:
        def begin(self):
            raise ConnectionError("database is down")

    out = tmp_path / "ledger"
    res = write_session_ledgers(
        acc, session, CFG, t_at(30), "exp-1", "shadow", out, tmp_path / "none.jsonl", None, Down()
    )
    assert (out / "acct_vpc.json").exists()
    assert "ConnectionError" in res["accounts"][rt.binding]["stored"]["error"]


def test_incidents_that_day_travel_with_the_numbers():
    class Store:
        def all_incidents(self):
            def inc(kind, account, day):
                return SimpleNamespace(
                    kind=kind, account=account, opened_at=pd.Timestamp(day, tz="UTC")
                )

            return [
                inc("STOCK_FEED_DOWN", None, "2026-10-26T14:00"),
                inc("DAILY_LIMIT_HALT", "acct:vpc", "2026-10-26T15:00"),
                inc("DAILY_LIMIT_HALT", "acct:orb", "2026-10-26T15:00"),
                inc("STOCK_FEED_DOWN", None, "2026-10-23T14:00"),
            ]

    got = incident_counts(Store(), date(2026, 10, 26), "acct:vpc")
    assert got == {"STOCK_FEED_DOWN": 1, "DAILY_LIMIT_HALT": 1}  # another day's and account's out
    assert incident_counts(None, date(2026, 10, 26), "acct:vpc") == {}
