"""Account runtime: routing, entry steps, atomic reservation, position loop, daily limit."""

import threading
from datetime import date

import pandas as pd
import pytest
import yaml

from tournament.accounts.reservations import (
    EXPOSURE_LIMIT,
    MAX_POSITIONS,
    ReservationLedger,
)
from tournament.accounts.runtime import AccountRouter, AccountRuntime, WrongAccount
from tournament.live.quotes import OptionFeed, usable_touch
from tournament.orders.config import load_orders_config
from tournament.orders.manager import OrderManager
from tournament.orders.model import Purpose, Status
from tournament.orders.sim import SimBroker
from tournament.orders.store import IntentStore, memory_engine
from tournament.risk.config import DEFAULT_PATH, load_risk_config

T0 = pd.Timestamp("2026-10-26T15:00:00Z")
TODAY = date(2026, 10, 26)
CALL, PUT = "SPY261026C00765000", "SPY261026P00765000"
RISK = load_risk_config()


def at(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


def chain():
    rows = []
    for k in range(760, 771):
        for r in "CP":
            sym = f"SPY261026{r}{k * 1000:08d}"
            rows.append({"symbol": sym, "expiration": TODAY, "strike": float(k), "right": r})
    return pd.DataFrame(rows)


class Market:
    """A tiny live market: the option feed the runtime reads and the modeled broker fills on."""

    def __init__(self):
        self.feed = OptionFeed(
            pd.Timedelta(seconds=5),
            pd.Timedelta(seconds=2),
            pd.Timedelta(seconds=60),
            5000,
            pd.Timedelta(seconds=0.25),
            600,
        )
        self.feed.on_connected(T0 - pd.Timedelta(minutes=5))

    def quote(self, symbol, bid, ask, now, ask_size=50):
        m = {"T": "q", "S": symbol, "t": now.isoformat(), "bp": bid, "bs": 20, "ap": ask,
             "as": ask_size, "bx": "C", "ax": "C", "c": "A"}  # fmt: skip
        self.feed.on_messages([m], now + pd.Timedelta(milliseconds=5))


def build(binding="acct:vpc", strategy="vpc", risk=RISK, market=None, engine=None, cash=10_000.0):
    market = market or Market()
    engine = engine or memory_engine()
    store = IntentStore(engine)
    ocfg = load_orders_config()
    sim = SimBroker(market.feed, ocfg.simulation, cash, risk.sizing.contract_multiplier)
    mgr = OrderManager(
        binding,
        sim,
        store,
        ocfg,
        sleep=lambda s: None,
        quotes=lambda s, now: usable_touch(market.feed.view(s, now)),
    )
    ledger = ReservationLedger(engine, risk.limits, risk.sizing.contract_multiplier)
    rt = AccountRuntime(
        binding, strategy, risk, mgr, store, ledger, market.feed, sim, chain(), TODAY,
        10_000.0, 10_000.0, at(23 * 60), advance=sim.advance,
    )  # fmt: skip
    return rt, sim, market


def prices(market, now, call=(1.43, 1.44), put=(1.14, 1.15)):
    for sym in (CALL, PUT):
        b, a = call if sym == CALL else put
        market.quote(sym, b, a, now)


def cand(cid="c1", direction="bullish", binding="acct:vpc", **kw):
    base = {
        "candidate_id": cid,
        "account_binding": binding,
        "underlying_direction": direction,
        "entry_reference": 765.3,
        "submission_eligible": True,
        "expires_at": at(62),
    }
    return {**base, **kw}


def take(rt, market, now=None, c=None):
    now = at(0) if now is None else now
    prices(market, now)
    return rt.on_candidate(now, c or cand())


def fill(rt, market, start=0.0):
    """Tick past the modeled fill delay with fresh quotes."""
    for s in (start + 1.5, start + 2.5):
        prices(market, at(s))
        rt.on_tick(at(s))


# ---- the happy path and AC-22 ----------------------------------------------------------------


def test_a_candidate_becomes_a_sized_reserved_order_then_a_position():
    rt, sim, m = build()
    d = take(rt, m)
    assert d.accepted and d.selection.symbol == CALL
    assert d.size.quantity == 13 and d.size.premium_at_risk == pytest.approx(1872.0)
    assert d.size.premium_at_risk <= RISK.sizing.premium_budget_pct * 10_000  # AC-22
    assert rt.ledger.exposure_usd("acct:vpc") == pytest.approx(1872.0)
    fill(rt, m)
    assert rt.store.filled_by_symbol("acct:vpc") == {CALL: 13}
    assert rt.ledger.held("acct:vpc")[0].amount_usd == pytest.approx(1872.0)


def test_bearish_candidates_buy_puts():
    rt, _, m = build()
    d = take(rt, m, c=cand(direction="bearish"))
    assert d.accepted and d.selection.symbol == PUT and d.size.quantity == 17


@pytest.mark.parametrize(
    "kw, now_s, reason",
    [
        ({"submission_eligible": False}, 0, "LATE_SIGNAL"),
        ({}, 70, "CANDIDATE_EXPIRED"),
    ],
)
def test_late_and_expired_candidates_are_refused(kw, now_s, reason):
    rt, _, m = build()
    d = take(rt, m, now=at(now_s), c=cand(**kw))
    assert not d.accepted and d.reasons == (reason,)


def test_no_budget_feasible_contract_is_rejected_with_a_reason():  # AC-22
    rt, _, m = build()
    prices(m, at(0), call=(24.98, 25.0))  # one contract costs $2,500 against a $2,000 budget
    d = rt.on_candidate(at(0), cand())
    assert not d.accepted and "PREMIUM_EXCEEDS_BUDGET" in d.reasons
    assert rt.ledger.held("acct:vpc") == []


def test_a_wide_spread_or_a_down_option_feed_blocks_entry():
    rt, _, m = build()
    prices(m, at(0), call=(1.20, 1.44))
    assert "SPREAD_TOO_WIDE_ABS" in rt.on_candidate(at(0), cand()).reasons
    rt2, _, m2 = build()
    prices(m2, at(0))
    m2.feed.on_disconnected(at(0.5))
    assert rt2.on_candidate(at(1), cand()).reasons == ("OPTION_FEED_DOWN",)


# ---- routing (FR-02, AC-01) ------------------------------------------------------------------


def test_wrong_binding_fails_closed_and_opposing_candidates_stay_in_their_accounts():
    engine = memory_engine()
    market = Market()
    a, _, _ = build("acct:vpc", "vpc", market=market, engine=engine)
    b, _, _ = build("acct:vmr", "vmr", market=market, engine=engine)
    router = AccountRouter({"acct:vpc": a, "acct:vmr": b})
    prices(market, at(0))
    up = router.route(at(0), cand("u", "bullish", "acct:vpc"))
    down = router.route(at(0), cand("d", "bearish", "acct:vmr"))
    assert up.selection.symbol == CALL and down.selection.symbol == PUT
    assert {i.account_binding for i in a.store.all_for("acct:vpc")} == {"acct:vpc"}
    assert [i.symbol for i in a.store.all_for("acct:vmr")] == [PUT]
    with pytest.raises(WrongAccount):
        router.route(at(0), cand("x", binding="acct:ghost"))
    with pytest.raises(WrongAccount):
        a.on_candidate(at(0), cand("y", binding="acct:vmr"))


# ---- atomic reservation (RK-03, RK-04, AC-04, AC-06) ----------------------------------------


def test_a_second_candidate_is_refused_while_the_first_is_held():
    rt, _, m = build()
    assert take(rt, m).accepted
    d2 = take(rt, m, now=at(1), c=cand("c2", expires_at=at(100)))
    assert not d2.accepted and MAX_POSITIONS in d2.reasons and EXPOSURE_LIMIT in d2.reasons


def test_concurrent_reservations_cannot_exceed_the_limit():  # AC-04
    engine = memory_engine()
    ledger = ReservationLedger(engine, RISK.limits, 100)
    ok, bad = [], []

    def go(i):
        r = ledger.reserve(T0, "acct:vpc", f"c{i}", 1500.0, 10, 10_000.0)
        (ok if r.ok else bad).append(r)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1 and len(bad) == 11
    assert ledger.exposure_usd("acct:vpc") == 1500.0


def test_asking_to_reserve_twice_is_one_claim():
    ledger = ReservationLedger(memory_engine(), RISK.limits, 100)
    a = ledger.reserve(T0, "acct:vpc", "c1", 1500.0, 10, 10_000.0)
    b = ledger.reserve(T0, "acct:vpc", "c1", 1500.0, 10, 10_000.0)
    assert a.ok and b.ok and len(ledger.held("acct:vpc")) == 1


def test_the_claim_follows_partial_fills_cancels_and_exits():  # RK-04, AC-06
    rt, sim, m = build()
    d = take(rt, m)
    cid = d.intent.client_order_id
    sim.orders[cid].update(filled=5, price=1.44, status=Status.PARTIALLY_FILLED)
    rt.ledger.refresh(at(1), "acct:vpc", rt.store)
    assert rt.ledger.held("acct:vpc")[0].amount_usd == pytest.approx(
        5 * 1.44 * 100 + 8 * 1.44 * 100
    )
    rt.orders.poll(at(7))  # the entry times out: the unfilled 8 are cancelled, the 5 kept
    rt.ledger.refresh(at(7), "acct:vpc", rt.store)
    assert rt.ledger.held("acct:vpc")[0].amount_usd == pytest.approx(5 * 1.44 * 100)
    # sell the 5 and the claim is released
    prices(m, at(8))
    rt.orders.place_exit(at(8), "c1", "vpc", CALL, "TARGET", 1.43, 1.44)
    for s in (9.5, 10.5):
        prices(m, at(s))
        rt.on_tick(at(s))
    assert rt.ledger.held("acct:vpc") == [] and rt.orders.position(CALL) == 0


def test_a_claim_that_never_became_an_order_lapses_and_an_unfilled_one_is_released():
    ledger = ReservationLedger(memory_engine(), RISK.limits, 100)
    store = IntentStore(memory_engine())
    ledger.reserve(T0, "acct:vpc", "c1", 1500.0, 10, 10_000.0)
    ledger.refresh(at(30), "acct:vpc", store)
    assert len(ledger.held("acct:vpc")) == 1
    ledger.refresh(at(61), "acct:vpc", store)  # ttl 60 s
    assert ledger.held("acct:vpc") == []


# ---- the position loop (EX-01 to EX-05, D-33, D-34) --------------------------------------------


def hold(rt, m):
    take(rt, m)
    fill(rt, m)
    assert rt.orders.position(CALL) == 13


def test_the_premium_stop_exits_at_the_configured_loss():
    rt, sim, m = build()
    hold(rt, m)
    prices(m, at(10), call=(1.16, 1.17))  # above the stop: 80% of 1.44 is 1.152
    rt.on_tick(at(10))
    assert rt.exits_requested == []
    prices(m, at(11), call=(1.15, 1.16))
    rt.on_tick(at(11))
    assert [r for _, _, r in rt.exits_requested] == ["PREMIUM_STOP"]
    for s in (12.5, 13.5):
        prices(m, at(s), call=(1.15, 1.16))
        rt.on_tick(at(s))
    assert rt.orders.position(CALL) == 0
    assert rt.realized_pnl() == pytest.approx((1.15 - 1.44) * 13 * 100)
    assert rt.halted is None  # a stopped trade (about -$377) stays inside the $500 limit


def test_the_cutoff_and_a_stop_together_make_one_exit_with_both_reasons():
    rt, sim, m = build()
    rt.cutoff_at = at(12)
    hold(rt, m)
    prices(m, at(12), call=(1.0, 1.01))
    rt.on_tick(at(12))
    sells = [i for i in rt.store.all_for("acct:vpc") if i.purpose is Purpose.EXIT]
    assert len(sells) == 1 and set(sells[0].reasons) == {"SESSION_CUTOFF", "PREMIUM_STOP"}


def test_every_exit_trigger_at_once_sells_the_position_only_once():  # AC-07
    rt, sim, m = build()
    rt.cutoff_at = at(12)
    hold(rt, m)
    prices(m, at(12), call=(1.0, 1.01))
    cand_id = rt._candidate_of(CALL)
    rt.on_reference_exit(at(12), cand_id, "TARGET")  # the strategy's own exit
    rt.close_position(at(12), CALL)  # the operator
    rt.flatten(at(12))  # the operator again
    rt.on_tick(at(12))  # the cutoff and the stop
    rt.on_tick(at(12.5))  # and the next tick, before any fill
    sells = [i for i in rt.store.all_for("acct:vpc") if i.purpose is Purpose.EXIT]
    assert len(sells) == 1 and sells[0].qty_intended <= 13
    assert {"REF_TARGET", "OPERATOR_CLOSE", "OPERATOR_FLATTEN"} <= set(sells[0].reasons)
    for s in (13, 14):
        prices(m, at(s), call=(1.0, 1.01))
        rt.on_tick(at(s))
    assert rt.orders.position(CALL) == 0  # sold once, never short


def test_a_quote_that_cannot_be_trusted_never_triggers_the_stop():
    rt, sim, m = build()
    hold(rt, m)
    prices(m, at(3))
    rt.on_tick(at(30))  # 27 s later the last quote is stale: unusable, so no decision on it
    assert rt.exits_requested == []


def test_the_daily_loss_limit_halts_entries_but_keeps_managing_the_position():
    rt, sim, m = build()
    hold(rt, m)
    prices(m, at(10), call=(1.05, 1.06))  # open loss (1.05-1.44)*1300 = -$507
    rt.on_tick(at(10))
    assert rt.halted == "DAILY_LIMIT" and rt.guard.breached_at is not None
    d = rt.on_candidate(at(10), cand("c2", expires_at=at(100)))
    assert not d.accepted and d.reasons == ("DAILY_LIMIT",)
    assert [r for _, _, r in rt.exits_requested] == ["PREMIUM_STOP"]  # still managed


def test_flatten_on_breach_is_a_config_choice(tmp_path):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["daily_loss"].update(on_breach="halt_and_flatten", limit_usd=100)
    raw["premium_stop"].update(loss_pct=0.9)  # so only the daily limit can fire here
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw))
    rt, sim, m = build(risk=load_risk_config(p))
    hold(rt, m)
    prices(m, at(10), call=(1.30, 1.31))  # -$182 open: over the $100 limit, above the stop
    rt.on_tick(at(10))
    assert [r for _, _, r in rt.exits_requested] == ["DAILY_LOSS_FLATTEN"]


def test_a_reference_exit_cancels_an_unfilled_entry():  # D-34
    rt, sim, m = build()
    take(rt, m)
    prices(m, at(0.5), call=(1.59, 1.60))  # the market ran away: the 1.44 limit isn't marketable
    rt.on_tick(at(1.5))
    assert rt.orders.position(CALL) == 0
    rt.on_reference_exit(at(2), "c1", "INVALIDATION")
    entry = rt.store.chain("acct:vpc", "c1", Purpose.ENTRY)[0]
    assert entry.status is Status.CANCELED and rt.exits_requested == []
    rt.ledger.refresh(at(3), "acct:vpc", rt.store)
    assert rt.ledger.held("acct:vpc") == []


def test_a_reference_exit_sells_a_filled_position():
    rt, sim, m = build()
    hold(rt, m)
    prices(m, at(20))
    rt.on_reference_exit(at(20), "c1", "TARGET")
    assert [r for _, _, r in rt.exits_requested] == ["REF_TARGET"]


def test_the_budget_is_a_config_value(tmp_path):
    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    raw["sizing"].update(premium_budget_pct=0.10)
    raw["limits"].update(max_open_premium_pct=0.10)
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(raw))
    rt, _, m = build(risk=load_risk_config(p))
    assert take(rt, m).size.quantity == 6
