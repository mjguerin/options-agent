"""OPRA option quotes: feed guard, wire format, quote gates, feed health, subscriptions."""

import asyncio
from datetime import date

import msgpack
import pandas as pd
import pytest

from tournament.live.option_stream import (
    AlpacaOptionStream,
    check_option_feed,
    option_url,
)
from tournament.live.quotes import (
    CROSSED,
    DELAYED_DELIVERY,
    FEED_DOWN,
    NO_ASK,
    NO_BID,
    NO_QUOTE,
    STALE_QUOTE,
    OptionFeed,
    quote_from,
)
from tournament.live.stream import FeedGuardError
from tournament.live.subscriptions import OptionUniverse, SubscriptionManager, parse_occ

T0 = pd.Timestamp("2026-10-26T14:00:00Z")
SYM = "SPY261026C00760000"


def sec(s: float) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=s)


def q(ts, bid=1.00, ask=1.05, symbol=SYM, bs=10, as_=12):
    return {
        "T": "q",
        "S": symbol,
        "t": ts.isoformat(),
        "bp": bid,
        "bs": bs,
        "ap": ask,
        "as": as_,
        "bx": "C",
        "ax": "C",
        "c": "A",
    }


def feed():
    f = OptionFeed(pd.Timedelta(seconds=5), pd.Timedelta(seconds=2), pd.Timedelta(seconds=10), 5000)
    f.on_connected(sec(-60))
    return f


# ---- feed guard (MD-09, AC-23) ----------------------------------------------------------


def test_paper_and_shadow_reject_the_indicative_feed():
    check_option_feed("opra", "paper")
    check_option_feed("opra", "shadow")
    check_option_feed("indicative", "test")
    for mode in ("paper", "shadow"):
        with pytest.raises(FeedGuardError):
            check_option_feed("indicative", mode)
    with pytest.raises(FeedGuardError):
        check_option_feed("iex", "paper")
    assert option_url("opra").endswith("/v1beta1/opra")


def test_the_stream_refuses_to_be_built_on_the_wrong_feed():
    with pytest.raises(FeedGuardError):
        AlpacaOptionStream("indicative", "k", "s", mode="paper", max_symbols=10, **_callbacks())


def _callbacks():
    n = lambda *a: None  # noqa: E731
    return dict(
        on_messages=n,
        on_connected=n,
        on_disconnected=n,
        on_tick=n,
        tick_s=0.25,
        backoff_s=(1,),
        ping_interval_s=10,
        handshake_timeout_s=10,
    )


# ---- wire format and subscription sync ----------------------------------------------------


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(msgpack.unpackb(data))


def stream(max_symbols=5):
    return AlpacaOptionStream(
        "opra", "k", "s", mode="shadow", max_symbols=max_symbols, **_callbacks()
    )


def test_messagepack_round_trip_and_content_type():
    s = stream()
    assert s.content_type == "application/msgpack"
    assert s.decode(s.encode({"action": "auth"})) == {"action": "auth"}


def test_sync_sends_only_the_difference_and_resubscribes_after_reconnect():
    s, ws = stream(), FakeWs()
    s.set_symbols({"A", "B"})
    asyncio.run(s.sync(ws))
    s.set_symbols({"B", "C"})
    asyncio.run(s.sync(ws))
    asyncio.run(s.sync(ws))  # nothing changed: nothing sent
    assert ws.sent == [
        {"action": "subscribe", "quotes": ["A", "B"]},
        {"action": "unsubscribe", "quotes": ["A"]},
        {"action": "subscribe", "quotes": ["C"]},
    ]
    ws2 = FakeWs()
    asyncio.run(s.on_authenticated(ws2))  # a fresh connection subscribes everything again
    assert ws2.sent == [{"action": "subscribe", "quotes": ["B", "C"]}]


def test_wildcard_and_over_limit_sets_are_refused():
    s = stream(max_symbols=2)
    with pytest.raises(FeedGuardError):
        s.set_symbols({"*"})
    with pytest.raises(FeedGuardError):
        s.set_symbols({"A", "B", "C"})


def test_server_confirmation_is_recorded():
    s = stream()
    s.on_subscription({"T": "subscription", "quotes": ["A"]})
    assert s.confirmed == {"A"}


# ---- quote gates (MD-04, EX-06) -----------------------------------------------------------


def test_a_fresh_valid_quote_is_usable():
    f = feed()
    f.on_messages([q(sec(0))], sec(0.1))
    v = f.view(SYM, sec(1))
    assert v.usable and v.reasons == () and v.quote.mid == pytest.approx(1.025)
    assert v.quote.spread == pytest.approx(0.05)


@pytest.mark.parametrize(
    "msg_kwargs, now, received, reason",
    [
        ({}, 7.0, 0.1, STALE_QUOTE),  # 7 s old
        ({}, 1.0, 3.5, DELAYED_DELIVERY),  # arrived 3.5 s after its timestamp
        ({"bid": 0.0}, 1.0, 0.1, NO_BID),
        ({"ask": 0.0}, 1.0, 0.1, NO_ASK),
        ({"bid": 1.10, "ask": 1.05}, 1.0, 0.1, CROSSED),
    ],
)
def test_each_unusable_reason(msg_kwargs, now, received, reason):
    f = feed()
    f.on_messages([q(sec(0), **msg_kwargs)], sec(received))
    v = f.view(SYM, sec(now))
    assert not v.usable and reason in v.reasons


def test_no_quote_and_out_of_order_quotes():
    f = feed()
    assert f.view(SYM, sec(0)).reasons == (NO_QUOTE,)
    f.on_messages([q(sec(5), bid=2.0, ask=2.1)], sec(5.1))
    f.on_messages([q(sec(3), bid=1.0, ask=1.1)], sec(5.2))  # older, arrives later
    assert f.book.quotes[SYM].bid == 2.0 and f.book.counts["out_of_order"] == 1


def test_a_disconnected_or_silent_feed_blocks_every_quote():
    f = feed()
    f.on_messages([q(sec(0))], sec(0.1))
    assert f.view(SYM, sec(1)).usable
    f.on_disconnected(sec(2))
    v = f.view(SYM, sec(3))
    assert not v.usable and FEED_DOWN in v.reasons
    f.on_connected(sec(4))
    f.on_messages([q(sec(4.5))], sec(4.6))
    assert f.view(SYM, sec(5)).usable
    # silent for longer than feed_silent_after while subscribed
    f.tick(sec(20), sec(-10), expecting_data=True)
    assert not f.entries_allowed(sec(20))
    f.on_messages([q(sec(21))], sec(21.1))
    assert f.entries_allowed(sec(21.2))


def test_nothing_subscribed_is_not_a_silent_feed():
    f = feed()
    f.tick(sec(100), sec(-10), expecting_data=False)
    assert f.entries_allowed(sec(100))


def test_quote_message_parsing_keeps_nanosecond_time():
    m = q(pd.Timestamp("2026-10-26T14:00:00.123456789Z"))
    assert quote_from(m, sec(1)).ts.nanosecond == 789


def test_integer_nanosecond_timestamps_from_messagepack():
    s = stream()
    ns = pd.Timestamp("2026-10-26T14:00:00.123456789Z").value
    raw = msgpack.packb([{"T": "q", "S": SYM, "t": msgpack.Timestamp.from_unix_nano(ns)}])
    (msg,) = s.decode(raw)
    assert msg["t"] == ns and quote_from({**msg, "bp": 1.0, "ap": 1.1}, sec(1)).ts.value == ns


def test_a_malformed_quote_is_counted_and_skipped():
    f = feed()
    f.on_messages([{"T": "q", "S": SYM}, q(sec(0))], sec(0.1))  # first has no timestamp
    assert f.book.counts["malformed"] == 1 and f.view(SYM, sec(1)).usable


# ---- subscriptions (MD-08, MD-10) ---------------------------------------------------------


def occ(strike, right="C", exp="261026"):
    return f"SPY{exp}{right}{int(strike * 1000):08d}"


def test_parse_occ_accepts_only_spy_options():
    assert parse_occ(occ(760.5, "P")) == (date(2026, 10, 26), "P", 760.5)
    assert parse_occ("QQQ261026C00500000") is None
    assert parse_occ("SPY") is None


def test_held_contracts_are_never_cut_and_non_spy_symbols_are_rejected():
    m = SubscriptionManager(max_symbols=3)
    p = m.plan(
        held=[occ(750)],
        candidates=[occ(751), occ(752)],
        watch=[occ(753), "QQQ261026C00500000"],
    )
    assert p.symbols == {occ(750), occ(751), occ(752)}
    assert p.dropped == (occ(753),) and p.rejected == ("QQQ261026C00500000",)
    assert not p.held_overflow


def test_held_overflow_is_reported_not_silently_cut():
    m = SubscriptionManager(max_symbols=1)
    p = m.plan(held=[occ(750), occ(751)], watch=[occ(752)])
    assert p.symbols == {occ(750), occ(751)} and p.held_overflow


def test_watch_window_is_the_nearest_strikes_for_the_chosen_expirations():
    rows = []
    for k in range(745, 756):
        for right in ("C", "P"):
            rows.append(
                {
                    "symbol": occ(k, right),
                    "expiration": date(2026, 10, 26),
                    "strike": float(k),
                    "right": right,
                }
            )
    rows.append(
        {
            "symbol": occ(750, "C", "261027"),
            "expiration": date(2026, 10, 27),
            "strike": 750.0,
            "right": "C",
        }
    )
    u = OptionUniverse(pd.DataFrame(rows))
    w = u.window(750.4, {date(2026, 10, 26)}, each_side=2)
    assert len(w) == 8 and occ(750, "C") in w and occ(751, "P") in w
    assert occ(749, "C") in w and occ(752, "C") in w and occ(753, "C") not in w
    assert occ(750, "C", "261027") not in w
    assert w[0].startswith("SPY261026") and parse_occ(w[0])[2] == 750.0  # nearest first
