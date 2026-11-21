"""Paper accounts (Step 4): real-broker accounts run beside the modeled ones, one per strategy.
A fake stands in for Alpaca; nothing here reaches a network."""

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
from test_account_runtime import CALL, Market, at, chain, prices
from test_shadow_accounts import record

from tournament.adapters.alpaca import AccountSummary
from tournament.live import paper_accounts as pa
from tournament.live.paper_accounts import PaperAccounts, connect_accounts
from tournament.marketdata.calendar import SessionDay
from tournament.orders.paper import PaperBroker, PaperChecks, account_problems
from tournament.orders.sim import SimBroker

CHECKS = PaperChecks(1, 10_000.0, 500.0, 2.0)
PC = {
    "enabled": True,
    "strategies": ["orb", "brt", "fbr"],
    "aliases": {"orb": "orb", "brt": "brt", "fbr": "fbr"},
    "tick_every_s": 1.0,
    "reconcile_every_s": 300,
    "cash_cache_s": 2.0,
    "min_options_level": 1,
    "expected_equity_usd": 10_000,
    "equity_tolerance_usd": 500,
}


def summary(number="****1111", **kw) -> AccountSummary:
    base = dict(
        account_number_masked=number,
        status="ACTIVE",
        currency="USD",
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        buying_power=Decimal("10000"),
        options_trading_level=3,
        trading_blocked=False,
    )
    return AccountSummary(**{**base, **kw})


def test_an_account_that_cannot_trade_is_refused_and_a_poor_balance_only_warns():
    assert account_problems(summary(), CHECKS) == ([], [])
    block, _ = account_problems(summary(status="ACCOUNT_UPDATED", trading_blocked=True), CHECKS)
    assert len(block) == 2
    assert account_problems(summary(options_trading_level=0), CHECKS)[0]
    assert account_problems(summary(options_trading_level=None), CHECKS)[0]
    block, warn = account_problems(summary(equity=Decimal("8000")), CHECKS)
    assert block == [] and len(warn) == 1


class FakeBroker:
    def __init__(self, number, **kw):
        self._s, self.cash_calls = summary(number, **kw), 0

    def summary(self):
        return self._s


def connect(monkeypatch, brokers: dict):
    def creds(alias):
        if alias not in brokers:
            raise RuntimeError(f"no keys for {alias}")
        return alias

    monkeypatch.setattr(pa, "load_alpaca_credentials", creds)
    monkeypatch.setattr(pa, "PaperBroker", lambda alias, ttl: brokers[alias])
    return connect_accounts(PC, ["orb", "brt", "fbr"])


def test_each_strategy_needs_its_own_account(monkeypatch):
    ok, report = connect(
        monkeypatch,
        {"orb": FakeBroker("****1"), "brt": FakeBroker("****2"), "fbr": FakeBroker("****3")},
    )
    assert sorted(ok) == ["brt", "fbr", "orb"] and all(r["ok"] for r in report.values())

    ok, report = connect(
        monkeypatch,
        {"orb": FakeBroker("****1"), "brt": FakeBroker("****1"), "fbr": FakeBroker("****3")},
    )
    assert sorted(ok) == ["fbr"]  # two strategies on one account would break isolation
    assert "shares account" in report["orb"]["problems"][0]


def test_missing_keys_and_a_blocked_account_leave_only_that_strategy_out(monkeypatch):
    ok, report = connect(
        monkeypatch, {"orb": FakeBroker("****1"), "brt": FakeBroker("****2", trading_blocked=True)}
    )
    assert sorted(ok) == ["orb"]
    assert "no keys for fbr" in report["fbr"]["problems"][0]
    assert report["brt"]["problems"] == ["trading is blocked on the account"]


def test_cash_is_cached_and_an_unreadable_account_reads_as_no_money():
    b = PaperBroker.__new__(PaperBroker)
    b.alias, b._ttl, b._cash, b._cash_at = "orb", 60.0, 0.0, float("-inf")
    calls = []

    def read():
        calls.append(1)
        return summary(cash=Decimal("1234.5"))

    b.summary = read
    assert b.cash() == 1234.5 and b.cash() == 1234.5 and len(calls) == 1
    b._cash_at = float("-inf")
    b.summary = lambda: (_ for _ in ()).throw(OSError("down"))
    assert b.cash() == 0.0  # never sized from a guess


def build(tmp_path, strategies=("orb",)):
    m = Market()
    collector = SimpleNamespace(feed=m.feed, universe=SimpleNamespace(contracts=chain()))
    day = SessionDay(
        pd.Timestamp("2026-10-26").date(), at(0).to_pydatetime(), at(23 * 60 + 600).to_pydatetime()
    )
    brokers = {
        s: SimBroker(
            m.feed,
            __import__("tournament.orders.config", fromlist=["x"]).load_orders_config().simulation,
            10_000.0,
            100,
        )
        for s in strategies
    }
    acc = PaperAccounts(
        brokers, day, collector, list(strategies), 10_000.0, 10, tmp_path / "p.sqlite", 1.0, 300.0
    )
    return acc, m


def test_a_shadow_candidate_reaches_only_its_strategys_paper_account(tmp_path):
    acc, m = build(tmp_path, ("orb",))
    prices(m, at(0))
    acc._candidate(record("a", "orb"))
    acc._candidate(record("b", "vpc"))  # no paper account: stays shadow-only, no error
    for s in (1.5, 2.5):
        prices(m, at(s))
        acc.tick(at(s))
    s = acc.summary()
    assert s["errors"] == [] and list(s["accounts"]) == ["paper:orb"]
    assert s["accounts"]["paper:orb"]["open_contracts"] == {CALL: 13}
    assert (tmp_path / "paper-decisions.jsonl").exists()


def test_reference_exits_of_a_shadow_only_strategy_are_ignored(tmp_path):
    acc, m = build(tmp_path, ("orb",))
    acc._reference_exit("vpc", "x", "TARGET", at(3))
    assert acc.errors == []


def test_the_shipped_config_names_the_three_paper_strategies():
    from tournament.live.runner import load_feed_config

    cfg = load_feed_config()["paper_accounts"]
    assert cfg["strategies"] == ["orb", "brt", "fbr"] and set(cfg["aliases"]) == set(
        cfg["strategies"]
    )
