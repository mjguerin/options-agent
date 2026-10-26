"""The modeled broker (D-66): fills honour the limit, the time, quote usability and quoted size."""

import pandas as pd
import pytest
from test_account_runtime import CALL, Market, at

from tournament.orders.broker import OrderRequest
from tournament.orders.config import load_orders_config
from tournament.orders.model import Status
from tournament.orders.sim import SimBroker

SIM = load_orders_config().simulation


def build(sim=SIM, cash=10_000.0):
    m = Market()
    return SimBroker(m.feed, sim, cash, 100), m


def req(cid="o1", side="buy", qty=13, limit=1.44, kind="limit"):
    return OrderRequest(cid, CALL, side, qty, kind, None if kind == "market" else limit, "day")


def submit(sim, r, t):
    sim.advance(t)
    sim.submit(r)


def test_a_marketable_buy_fills_one_second_after_submission_at_the_ask():
    sim, m = build()
    m.quote(CALL, 1.43, 1.44, at(0))
    submit(sim, req(), at(0))
    sim.advance(at(0.9))
    assert sim.get_by_client_id("o1").filled_qty == 0
    m.quote(CALL, 1.43, 1.44, at(1.0))
    sim.advance(at(1.0))
    o = sim.get_by_client_id("o1")
    assert (o.status, o.filled_qty, o.filled_avg_price) == (Status.FILLED, 13, 1.44)
    assert sim.cash() == pytest.approx(10_000 - 13 * 1.44 * 100)


def test_a_buy_below_the_ask_does_not_fill_until_the_ask_comes_to_it():
    sim, m = build()
    m.quote(CALL, 1.49, 1.50, at(0))
    submit(sim, req(limit=1.44), at(0))
    sim.advance(at(1.0))
    assert sim.get_by_client_id("o1").filled_qty == 0
    m.quote(CALL, 1.43, 1.44, at(2.0))
    sim.advance(at(2.1))  # quotes arrive 5 ms after their timestamp
    assert sim.get_by_client_id("o1").status is Status.FILLED


def test_the_first_attempt_never_uses_a_quote_that_arrived_after_the_delay():
    sim, m = build()
    m.quote(CALL, 1.49, 1.50, at(0.5))  # what the market looked like at submission + 1 s
    submit(sim, req(limit=1.44), at(0))
    m.quote(CALL, 1.39, 1.40, at(1.5))  # a better quote, but later
    sim.advance(at(2.0))  # the clock ticked late
    assert sim.get_by_client_id("o1").filled_qty == 0  # judged on the quote held at t = 1.0
    sim.advance(at(3.0))  # later attempts do look at the market as it is then
    o = sim.get_by_client_id("o1")
    assert o.status is Status.FILLED and o.filled_avg_price == 1.40


def test_a_stale_or_unusable_quote_never_fills():
    sim, m = build()
    m.quote(CALL, 1.43, 1.44, at(0))
    submit(
        sim, req(), at(10)
    )  # at the order's moment the only quote is 10 s old: past the 5 s gate
    sim.advance(at(11.0))
    assert sim.get_by_client_id("o1").filled_qty == 0
    m.quote(CALL, 1.50, 1.44, at(12.0))  # a fresh quote, but crossed
    sim.advance(at(12.1))
    assert sim.get_by_client_id("o1").filled_qty == 0
    m.quote(CALL, 1.43, 1.44, at(13.0))  # a good one
    sim.advance(at(13.1))
    assert sim.get_by_client_id("o1").status is Status.FILLED


def test_a_fill_never_exceeds_the_displayed_size_and_the_rest_fills_later():
    sim, m = build()
    m.quote(CALL, 1.43, 1.44, at(0), ask_size=5)
    submit(sim, req(qty=13), at(0))
    sim.advance(at(1.0))
    o = sim.get_by_client_id("o1")
    assert (o.status, o.filled_qty) == (Status.PARTIALLY_FILLED, 5)
    m.quote(CALL, 1.43, 1.44, at(2.0), ask_size=5)
    sim.advance(at(2.0))
    assert sim.get_by_client_id("o1").filled_qty == 10
    m.quote(CALL, 1.43, 1.44, at(3.0), ask_size=50)
    sim.advance(at(3.0))
    o = sim.get_by_client_id("o1")
    assert (o.status, o.filled_qty, o.filled_avg_price) == (Status.FILLED, 13, 1.44)


def test_the_size_cap_is_a_config_value():
    sim, m = build(SIM.model_copy(update={"size_cap": "none"}))
    m.quote(CALL, 1.43, 1.44, at(0), ask_size=2)
    submit(sim, req(qty=13), at(0))
    sim.advance(at(1.0))
    assert sim.get_by_client_id("o1").filled_qty == 13


def test_a_sell_fills_at_the_bid_only_if_the_bid_reaches_the_limit():
    sim, m = build()
    m.quote(CALL, 1.20, 1.21, at(0))
    submit(sim, req("s1", "sell", 5, limit=1.30), at(0))
    sim.advance(at(1.0))
    assert sim.get_by_client_id("s1").filled_qty == 0
    m.quote(CALL, 1.31, 1.32, at(2.0))
    sim.advance(at(2.1))
    s = sim.get_by_client_id("s1")
    assert s.status is Status.FILLED and s.filled_avg_price == 1.31


def test_a_market_order_fills_at_the_touch_and_a_cancel_stops_fills():
    sim, m = build()
    m.quote(CALL, 1.43, 1.44, at(0))
    submit(sim, req("m1", "sell", 3, kind="market"), at(0))
    submit(sim, req("c1", qty=4), at(0))
    sim.cancel(sim.get_by_client_id("c1").broker_order_id)
    sim.advance(at(1.0))
    assert sim.get_by_client_id("m1").filled_avg_price == 1.43
    c = sim.get_by_client_id("c1")
    assert c.status is Status.CANCELED and c.filled_qty == 0


def test_the_model_never_fills_without_a_quote_and_the_id_is_unique():
    sim, m = build()
    submit(sim, req(), at(0))
    sim.advance(at(5.0))
    assert sim.get_by_client_id("o1").filled_qty == 0
    from tournament.orders.broker import BrokerReject

    with pytest.raises(BrokerReject):
        sim.submit(req())
    assert pd.Timestamp(at(0)) == at(0)
