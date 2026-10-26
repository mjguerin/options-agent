"""The ledgers (AC-11, AC-12, AC-17): synthetic trades with hand-computed answers for fills, cash,
fees, open positions, equity and drawdown; the evaluation re-pricing; stress; ranking; and the
append-only store."""

import json

import pandas as pd
import pytest
import yaml

from tournament.ledger.build import NO_EXIT_QUOTE, OPEN_AT_END, broker_fills, build_trades
from tournament.ledger.config import (
    DEFAULT_PATH,
    LedgerConfigError,
    load_ledger_config,
)
from tournament.ledger.metrics import daily_metrics, equity_metrics, trade_metrics
from tournament.ledger.rank import gate_results, rank_accounts
from tournament.ledger.session import build_session_ledger
from tournament.ledger.store import (
    latest_sessions,
    ledger_equity,
    ledger_fills,
    ledger_trades,
    write_session,
)
from tournament.orders.model import Intent, Purpose, Status
from tournament.orders.store import IntentStore, memory_engine

T0 = pd.Timestamp("2026-10-26T15:00:00Z")
ACCT = "acct:vpc"
CALL = "SPY261026C00765000"
CALL2 = "SPY261026C00766000"
BASE = load_ledger_config()
CFG = BASE.model_copy(
    update={"costs": BASE.costs.model_copy(update={"commission_per_contract": 0.65})}
)  # a cost model, so the fee arithmetic is visible


def at(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


def order(store, cid, purpose, side, symbol, qty, filled, price, t, quote=None, reasons=()):
    """One order with its history: an intent event (with the decision quote), a fill, a status."""
    n = len(store.all_for(ACCT))
    i = Intent(
        f"o{n}", ACCT, cid, "vpc", purpose, side, symbol, qty, "limit", price or 1.0, "day",
        Status.PENDING_SUBMIT, at(t), submitted_at=at(t), reasons=tuple(reasons),
    )  # fmt: skip
    store.insert(i)
    detail = {"qty": qty}
    if quote:
        detail.update(bid=quote[0], ask=quote[1])
    else:
        detail.update(bid=None, ask=None)
    store.event(i.client_order_id, at(t), "intent", **detail)
    if filled:
        store.event(i.client_order_id, at(t + 1), "status", status="FILLED", filled=filled)
    store.save(
        i.with_(
            status=Status.FILLED if filled == qty else Status.CANCELED,
            qty_filled=filled,
            avg_fill_price=price if filled else None,
        )
    )


@pytest.fixture()
def store():
    s = IntentStore(memory_engine())
    # A: 10 contracts 1.00 -> 1.20; the decision quotes are 0.99/1.00 and 1.19/1.21
    order(s, "A", Purpose.ENTRY, "buy", CALL, 10, 10, 1.00, 0, quote=(0.99, 1.00))
    order(
        s,
        "A",
        Purpose.EXIT,
        "sell",
        CALL,
        10,
        10,
        1.20,
        100,
        quote=(1.19, 1.21),
        reasons=("TARGET",),
    )
    # B: 5 contracts 2.00 -> 1.50, a loss; quotes 1.99/2.00 and 1.49/1.51
    order(s, "B", Purpose.ENTRY, "buy", CALL2, 5, 5, 2.00, 200, quote=(1.99, 2.00))
    order(
        s,
        "B",
        Purpose.EXIT,
        "sell",
        CALL2,
        5,
        5,
        1.50,
        300,
        quote=(1.49, 1.51),
        reasons=("PREMIUM_STOP",),
    )
    # C: 3 contracts 1.00, still open at the end
    order(s, "C", Purpose.ENTRY, "buy", CALL, 3, 3, 1.00, 400, quote=(0.99, 1.00))
    # D: an entry that never filled: not a trade
    order(s, "D", Purpose.ENTRY, "buy", CALL, 4, 0, None, 500, quote=(0.99, 1.00))
    # E: 2 contracts 1.00 -> 0.80 sold to the market with no usable quote
    order(s, "E", Purpose.ENTRY, "buy", CALL2, 2, 2, 1.00, 600, quote=(0.99, 1.00))
    order(
        s,
        "E",
        Purpose.EXIT,
        "sell",
        CALL2,
        2,
        2,
        0.80,
        700,
        quote=None,
        reasons=("SESSION_CUTOFF",),
    )
    return s


def trades_by_id(store, cfg=CFG):
    return {t.candidate_id: t for t in build_trades(store, ACCT, cfg)}


# ---- the broker ledger ----


def test_fills_and_the_cash_they_moved_are_exact(store):
    fills = broker_fills(store, ACCT, 100)
    assert [(f.candidate_id, f.side, f.qty, f.price, f.cash_usd) for f in fills] == [
        ("A", "buy", 10, 1.00, -1000.0),
        ("A", "sell", 10, 1.20, 1200.0),
        ("B", "buy", 5, 2.00, -1000.0),
        ("B", "sell", 5, 1.50, 750.0),
        ("C", "buy", 3, 1.00, -300.0),
        ("E", "buy", 2, 1.00, -200.0),
        ("E", "sell", 2, 0.80, 160.0),
    ]  # D never filled: no fill, no trade


def test_a_buy_that_never_filled_is_not_a_trade_and_an_open_one_is_flagged(store):
    t = trades_by_id(store)
    assert set(t) == {"A", "B", "C", "E"}
    assert t["C"].open_qty == 3 and t["C"].closed_qty == 0 and OPEN_AT_END in t["C"].flags
    assert t["A"].open_qty == 0 and t["A"].exit_reasons == ["TARGET"]


# ---- the evaluation ledger ----


def test_the_evaluation_prices_each_trade_from_its_decision_quotes(store):
    a = trades_by_id(store)["A"]
    assert (a.broker_gross_usd, a.eval_entry, a.eval_exit) == (pytest.approx(200.0), 1.00, 1.19)
    assert a.eval_gross_usd == pytest.approx(190.0)  # bought at the ask, sold at the bid
    assert a.fees_usd == pytest.approx(13.0)  # 0.65 x (10 + 10)
    assert a.net_eval_usd == pytest.approx(177.0)
    assert a.holding_s == 100.0  # entry filled at 1 s, exit filled at 101 s


def test_a_decision_with_no_usable_quote_is_flagged_not_priced_from_thin_air(store):
    e = trades_by_id(store)["E"]
    assert NO_EXIT_QUOTE in e.flags and e.eval_exit == 0.80  # the broker's price, labelled
    assert e.eval_gross_usd == pytest.approx(-40.0)


def test_stress_scenarios_use_the_same_trades_with_harsher_assumptions(store):
    a = trades_by_id(store)["A"]
    assert a.stress_net_usd["base"] == pytest.approx(177.0)
    assert a.stress_net_usd["plus_1_tick"] == pytest.approx(157.0)  # 1.01 in, 1.18 out
    assert a.stress_net_usd["plus_2_ticks"] == pytest.approx(137.0)
    assert a.stress_net_usd["fees_x2"] == pytest.approx(164.0)  # 190 less 26


def test_slippage_and_costs_are_config_values(store, tmp_path):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["evaluation"]["entry_slippage_ticks"] = 2
    raw["evaluation"]["exit_slippage_ticks"] = 1
    p = tmp_path / "l.yaml"
    p.write_text(yaml.safe_dump(raw))
    cfg = load_ledger_config(p)
    a = trades_by_id(store, cfg)["A"]
    assert (a.eval_entry, a.eval_exit) == (pytest.approx(1.02), pytest.approx(1.18))
    with pytest.raises(LedgerConfigError):
        raw["stress"] = [{"name": "harsh", "extra_slippage_ticks": 1, "fee_multiplier": 1.0}]
        p.write_text(yaml.safe_dump(raw))
        load_ledger_config(p)  # a stress list without 'base' is refused


# ---- the session ledger: reconciliation, summary, metrics ----


def session_ledger(store, cash=None, marks=None, equity=None, cfg=CFG):
    fills = broker_fills(store, ACCT, 100)
    rebuilt = 10_000.0 + sum(f.cash_usd for f in fills)
    mark_c = {"symbol": CALL, "qty": 3, "avg_entry": 1.00, "bid": 1.10, "unrealized_usd": 30.0}
    return build_session_ledger(
        store, ACCT, cfg, session_date="2026-10-26", start_equity=10_000.0,
        broker_cash=rebuilt if cash is None else cash,
        broker_positions={CALL: 3},
        open_marks=[mark_c] if marks is None else marks,
        equity=pd.DataFrame() if equity is None else equity,
        operational={"uncertain_slots": 0, "incidents": {}},
    )  # fmt: skip


def test_the_books_reconcile_to_the_cent(store):  # AC-11
    led = session_ledger(store)
    r = led["reconciliation"]
    assert r["ok"] and r["checks"]["cash"]["difference_usd"] == pytest.approx(0.0)
    assert r["checks"]["realized"]["ok"] and r["checks"]["positions"]["ok"]
    s = led["summary"]
    assert s["broker_realized_gross_usd"] == pytest.approx(200.0 - 250.0 - 40.0 + 0.0 + 0.0)
    assert s["fees_usd"] == pytest.approx(13.0 + 6.5 + 1.95 + 2.6)
    assert s["net_realized_eval_usd"] == pytest.approx(177.0 - 261.5 - 42.6)
    assert s["open_estimate_usd"] == 30.0 and s["net_pnl_est_usd"] == pytest.approx(-97.1)


def test_a_cash_difference_is_an_accounting_incident_not_something_netted_away(store):
    led = session_ledger(store, cash=9_990.0)
    assert not led["reconciliation"]["ok"]
    assert led["reconciliation"]["checks"]["cash"]["difference_usd"] == pytest.approx(
        9_990.0 - (10_000.0 - 1000 + 1200 - 1000 + 750 - 300 - 200 + 160)
    )


def test_a_position_the_broker_does_not_have_is_a_reconciliation_failure(store):
    fills = broker_fills(store, ACCT, 100)
    led = build_session_ledger(
        store, ACCT, CFG, session_date="2026-10-26", start_equity=10_000.0,
        broker_cash=10_000.0 + sum(f.cash_usd for f in fills), broker_positions={},
        open_marks=[], equity=pd.DataFrame(), operational={},
    )  # fmt: skip
    assert led["reconciliation"]["checks"]["positions"]["differences"] == {CALL: [3, 0]}


def test_the_conservative_risk_figure_values_an_unmarked_position_at_zero(store):
    blind = {"symbol": CALL, "qty": 3, "avg_entry": 1.00, "bid": None, "unrealized_usd": None}
    s = session_ledger(store, marks=[blind])["summary"]
    assert s["unmarked_positions"] == 1 and s["open_estimate_usd"] == 0.0
    assert s["open_risk_usd"] == pytest.approx(-300.0)  # the premium counted as lost
    assert s["net_pnl_risk_usd"] == pytest.approx(-127.1 - 300.0)


def test_the_trade_metrics_and_their_denominators(store):
    m = session_ledger(store)["metrics"]
    assert (m["trades"], m["closed_trades"], m["open_trades"]) == (4, 3, 1)
    assert m["win_rate"] == pytest.approx(1 / 3)  # wins over CLOSED trades; the open one is out
    assert m["avg_net_trade_usd"] == pytest.approx((177.0 - 261.5 - 42.6) / 3)
    assert m["payoff_ratio"] == pytest.approx(177.0 / ((261.5 + 42.6) / 2))
    assert m["profit_factor"] == pytest.approx(177.0 / (261.5 + 42.6))
    assert m["largest_loss_usd"] == pytest.approx(-261.5)
    assert m["premium_deployed_usd"] == pytest.approx(1000 + 1000 + 300 + 200)
    assert m["turnover_x_start_equity"] == pytest.approx((2500 + 1200 + 750 + 160) / 10_000)


def test_undefined_metrics_are_none_not_zero():
    m = trade_metrics([], CFG, 10_000.0)
    assert m["win_rate"] is None and m["payoff_ratio"] is None and m["profit_factor"] is None
    only_wins = [{"closed_qty": 1, "net_eval_usd": 50.0, "eval_gross_usd": 50.0, "fees_usd": 0.0,
                  "holding_s": 10.0, "premium_usd": 100.0, "broker_exit": 1.0}]  # fmt: skip
    w = trade_metrics(only_wins, CFG, 10_000.0)
    assert w["profit_factor"] is None and w["payoff_ratio"] is None and w["win_rate"] == 1.0


def test_drawdown_comes_from_the_equity_series_and_exposure_time_from_its_snapshots():
    idx = pd.to_datetime([at(0), at(30), at(60), at(90)])
    eq = pd.DataFrame(
        {"estimated_usd": [0.0, 50.0, -30.0, 20.0], "exposure_usd": [0.0, 500.0, 500.0, 500.0]},
        index=idx,
    )
    m = equity_metrics(eq, 10_000.0)
    assert m["max_drawdown_usd"] == pytest.approx(-80.0)  # from the 10,050 peak to 9,970
    assert m["max_drawdown_pct_of_start"] == pytest.approx(-0.008)
    assert m["exposure_time_share"] == pytest.approx(0.75)
    assert equity_metrics(pd.DataFrame(), 10_000.0)["max_drawdown_usd"] is None


def test_daily_figures_count_active_days_and_the_worst_day():
    days = [
        {"net_pnl_est_usd": 100.0, "trades": 2},
        {"net_pnl_est_usd": -250.0, "trades": 1},
        {"net_pnl_est_usd": 0.0, "trades": 0},
    ]
    d = daily_metrics(days, 10_000.0)
    assert (d["active_days"], d["worst_day_usd"]) == (2, -250.0)
    assert d["daily_returns"] == [0.01, -0.025, 0.0]


# ---- ranking and promotion (AC-12) ----


def entry(account, net, closed=5, dd=-0.02, ok=True):
    return {
        "summary": {
            "account": account, "start_equity_usd": 10_000.0, "net_pnl_est_usd": net,
            "stress_net_usd": {"base": net, "plus_2_ticks": net - 40},
        },
        "metrics": {"closed_trades": closed, "active_days": 3, "max_drawdown_pct_of_start": dd},
        "reconciliation": {"checks": {"cash": {"ok": ok}}},
    }  # fmt: skip


def test_an_unprofitable_leader_leads_but_is_not_promotion_qualified():
    rows = rank_accounts([entry("acct:orb", -50.0), entry("acct:vpc", -10.0)], CFG)
    assert [r["account"] for r in rows] == ["acct:vpc", "acct:orb"]
    assert rows[0]["rank"] == 1 and rows[0]["leader_not_qualified"] is True
    assert not rows[0]["promotion_qualified"]
    assert rows[0]["gates"]["min_net_pnl_usd"]["status"] == "fail"


def test_a_profitable_leader_with_clean_books_qualifies_and_unset_gates_are_not_evaluated():
    rows = rank_accounts([entry("acct:vpc", 80.0), entry("acct:orb", 20.0)], CFG)
    assert rows[0]["promotion_qualified"] and not rows[0]["leader_not_qualified"]
    g = rows[0]["gates"]
    assert g["min_trades"]["status"] == "not_evaluated" and g["min_net_pnl_usd"]["status"] == "pass"
    assert g["max_unresolved_accounting_incidents"]["status"] == "pass"


def test_an_accounting_difference_fails_the_account_even_when_it_leads():
    rows = rank_accounts([entry("acct:vpc", 80.0, ok=False)], CFG)
    assert not rows[0]["promotion_qualified"]
    assert (
        gate_results(entry("acct:vpc", 80.0, ok=False), CFG)["max_unresolved_accounting_incidents"][
            "status"
        ]
        == "fail"
    )


def test_with_no_gate_set_nothing_qualifies(tmp_path):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["promotion_gates"] = {k: None for k in raw["promotion_gates"]}
    p = tmp_path / "l.yaml"
    p.write_text(yaml.safe_dump(raw))
    rows = rank_accounts([entry("acct:vpc", 999.0)], load_ledger_config(p))
    assert not rows[0]["promotion_qualified"]  # nothing was evaluated, so nothing was passed


# ---- the append-only store ----


def test_a_session_ledger_is_written_once_and_a_correction_appends_a_revision(store):
    engine = memory_engine()
    idx = pd.to_datetime([at(0), at(30)])
    eq = pd.DataFrame(
        {"realized_usd": [0.0, 0.0], "unrealized_usd": [0.0, 30.0], "estimated_usd": [0.0, 30.0],
         "risk_usd": [0.0, 30.0], "exposure_usd": [0.0, 300.0]}, index=idx,
    )  # fmt: skip
    led = session_ledger(store, equity=eq)
    lid, rev, created = write_session(engine, "exp-1", "shadow", led, at(1000))
    assert (rev, created) == (1, True)
    again = write_session(engine, "exp-1", "shadow", led, at(2000))
    assert again == (lid, 1, False)  # identical content: nothing changes
    changed = session_ledger(store, equity=eq, marks=[])  # a correction: the open mark is gone
    _, rev2, created2 = write_session(engine, "exp-1", "shadow", changed, at(3000))
    assert (rev2, created2) == (2, True)
    with engine.connect() as c:
        from sqlalchemy import func, select

        assert c.execute(select(func.count()).select_from(ledger_trades)).scalar() == 8  # 4 + 4
        assert c.execute(select(func.count()).select_from(ledger_fills)).scalar() == 14
        assert c.execute(select(func.count()).select_from(ledger_equity)).scalar() == 4
    newest = latest_sessions(engine, "exp-1", "shadow")
    assert len(newest) == 1 and newest[0]["revision"] == 2
    assert newest[0]["summary"]["open_estimate_usd"] == 0.0 and json.dumps(newest[0]["metrics"])
    assert latest_sessions(engine, "exp-2", "shadow") == []
