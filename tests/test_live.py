"""Live feed (Step 3): live/batch bar parity, ingestion boundary, late inputs, corrections,
feed health, halts, deduplication, pre-market and the feed guard. All on a synthetic clock."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tournament.live.session import LiveSession
from tournament.live.stream import FeedGuardError, check_feed, stock_url
from tournament.marketdata.bars import BAR_SCHEMA, build_session_bars
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage

CFG = EligibilityConfig()
OPEN = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
DAY = SessionDay(date(2026, 10, 26), OPEN, OPEN + timedelta(minutes=6))
PRE = pd.Timestamp(DAY.extended_start_utc) - pd.Timedelta(minutes=1)  # connected before 04:00
_ids = iter(range(1, 10**6))


def at(minute: int, sec: float) -> pd.Timestamp:
    """Time `sec` seconds into slot `minute`."""
    return pd.Timestamp(OPEN + timedelta(minutes=minute - 1, seconds=sec))


def msg(ts, price, size=100, c=(" ",), x="P", i=None):
    return {
        "T": "t",
        "S": "SPY",
        "t": ts.isoformat(),
        "p": price,
        "s": size,
        "x": x,
        "c": list(c),
        "i": next(_ids) if i is None else i,
        "z": "B",
    }


def session(connect=PRE, stale=timedelta(hours=1)):
    """Synthetic sessions go quiet for minutes; real SPY never does in regular hours, so the
    stale rule is switched off except where a test is about it."""
    s = LiveSession(
        DAY,
        CFG,
        finalize_delay=timedelta(seconds=2),
        halt_codes=frozenset({"H", "P"}),
        resume_codes=frozenset({"T"}),
        stale_after=stale,
    )
    s.on_connected(connect)
    return s


def feed(s, m, received=None):
    """Deliver one message, received a little after its own timestamp by default."""
    s.on_messages([m], received or pd.Timestamp(m["t"]) + pd.Timedelta(milliseconds=50))


def bars(s):
    return pd.DataFrame(s.bars).set_index("slot")


TRADES = [  # (slot, sec, price, size, conditions)
    (1, 1, 500.00, 100, (" ",)),
    (1, 20, 500.50, 50, ("I",)),  # odd lot: volume only
    (1, 40, 500.25, 200, (" ", "Z")),  # NOTE_2 after a qualifying last: not last-eligible
    (3, 5, 501.00, 300, (" ",)),
    (3, 30, 500.10, 100, ("T",)),  # Form T in the regular session: excluded from price
    (5, 2, 499.90, 100, (" ", "Y")),  # unknown condition: quarantined
    (5, 9, 499.80, 100, (" ",)),
]


def test_on_time_session_matches_the_batch_builder_exactly():
    s = session()
    rows = []
    for m, sec, p, size, c in TRADES:
        mm = msg(at(m, sec), p, size, c)
        rows.append({**mm, "ts": at(m, sec)})
        feed(s, mm)
    s.tick(pd.Timestamp(DAY.close_utc) + pd.Timedelta(seconds=3))
    batch = (
        build_session_bars(
            pd.DataFrame(
                {
                    "ts": [r["ts"] for r in rows],
                    "price": [r["p"] for r in rows],
                    "size": [r["s"] for r in rows],
                    "exchange": [r["x"] for r in rows],
                    "conditions": [r["c"] for r in rows],
                    "trade_id": [r["i"] for r in rows],
                    "tape": [r["z"] for r in rows],
                }
            ),
            DAY,
            CFG,
            Usage.LIVE,
        )
        .to_pandas()
        .set_index("slot")
    )
    live = bars(s)
    assert len(live) == 6
    for c in BAR_SCHEMA.names[1:]:
        for m in range(1, 7):
            a, b = batch.at[m, c], live.at[m, c]
            if isinstance(a, float) and np.isnan(a):
                assert np.isnan(b), (c, m)
            elif c == "flags":
                assert list(a) == list(b), (c, m)
            else:
                assert a == b, (c, m)
    assert list(live["interval_status"]) == [
        "PRESENT",
        "KNOWN_NO_ELIGIBLE_TRADES",
        "PRESENT",
        "KNOWN_NO_ELIGIBLE_TRADES",
        "DATA_MISSING_OR_UNCERTAIN",
        "KNOWN_NO_ELIGIBLE_TRADES",
    ]


def test_input_received_after_scheduled_finalization_is_late_and_moves_forward():
    s = session()
    feed(s, msg(at(1, 10), 500.0))
    # a slot-1 trade received at bar_end + 2.5 s: slot 1 was already frozen at + 2 s
    feed(s, msg(at(1, 50), 510.0, 400), received=at(2, 2.5))
    s.tick(at(3, 2.1))
    b = bars(s)
    assert b.at[1, "close"] == 500.0 and b.at[1, "volume"] == 100  # bar unchanged
    assert b.at[2, "cum_total_volume"] == 500 and b.at[2, "cum_vwap_volume"] == 500
    assert b.at[2, "late_total"] == 1 and b.at[2, "volume"] == 0
    assert b.at[2, "finalized_at"] >= b.at[2, "scheduled_finalization_at"]


def test_trade_received_before_the_boundary_counts_in_its_slot():
    s = session()
    feed(s, msg(at(1, 59.9), 500.0), received=at(2, 1.9))  # 1.9 s after bar_end: in time
    s.tick(at(2, 2.0))
    assert bars(s).at[1, "close"] == 500.0


def test_duplicate_deliveries_are_one_event():
    s = session()
    m = msg(at(1, 5), 500.0, i=42)
    feed(s, m)
    feed(s, m)
    s.tick(at(2, 3))
    assert bars(s).at[1, "volume"] == 100 and s.counts["duplicates"] == 1


def test_disconnect_marks_slots_uncertain_and_vwap_stays_uncertain():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    s.on_disconnected(at(2, 30))
    s.on_connected(at(3, 10))
    feed(s, msg(at(3, 20), 501.0))
    feed(s, msg(at(4, 20), 502.0))
    s.tick(at(5, 3))
    b = bars(s)
    assert list(b["interval_status"]) == [
        "PRESENT",
        "DATA_MISSING_OR_UNCERTAIN",
        "DATA_MISSING_OR_UNCERTAIN",
        "PRESENT",
    ]
    assert list(b["vwap_uncertain"]) == [False, True, True, True]  # sticky (SD-04)
    assert s.health.unhealthy[-1][2] == "disconnected"


def test_disconnect_after_bar_end_but_before_finalization_still_counts():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    s.on_disconnected(at(2, 1.0))  # slot 1's input could still arrive until bar_end + 2 s
    s.tick(at(2, 3))
    assert bars(s).at[1, "interval_status"] == "DATA_MISSING_OR_UNCERTAIN"


def test_silence_beyond_the_stale_threshold_is_uncertain():
    s = session(stale=timedelta(seconds=5))
    feed(s, msg(at(1, 5), 500.0))
    s.tick(at(1, 11))  # 6 s without any message while connected
    feed(s, msg(at(1, 40), 500.5))
    s.tick(at(2, 3))
    s.tick(at(3, 3))
    b = bars(s)
    # the stale interval starts at the last message (slot 1) and closes when input resumes
    assert b.at[1, "interval_status"] == "DATA_MISSING_OR_UNCERTAIN"
    assert s.health.unhealthy[-1][2] == "stale"


def test_not_connected_at_start_is_uncertain():
    s = session(connect=at(2, 30))
    s.tick(at(3, 3))
    assert list(bars(s)["interval_status"]) == ["DATA_MISSING_OR_UNCERTAIN"] * 2


def test_halt_and_resume():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    s.on_messages([{"T": "s", "S": "SPY", "sc": "H", "t": at(2, 10).isoformat()}], at(2, 10.1))
    s.on_messages([{"T": "s", "S": "SPY", "sc": "T", "t": at(4, 0).isoformat()}], at(4, 0.1))
    feed(s, msg(at(4, 5), 500.0))
    s.tick(at(5, 3))
    assert list(bars(s)["interval_status"]) == [
        "PRESENT",
        "HALTED",
        "HALTED",
        "PRESENT",
    ]


def test_cancel_and_correction_of_pending_trades_edit_the_slot():
    s = session()
    feed(s, msg(at(1, 5), 500.0, i=7))
    feed(s, msg(at(1, 6), 505.0, i=8))
    s.on_messages(
        [{"T": "x", "S": "SPY", "x": "P", "i": 8, "a": "C", "t": at(1, 9).isoformat()}], at(1, 9)
    )
    s.on_messages(
        [
            {
                "T": "c",
                "S": "SPY",
                "x": "P",
                "oi": 7,
                "ci": 9,
                "cp": 501.0,
                "cs": 100,
                "cc": [" "],
                "t": at(1, 10).isoformat(),
                "z": "B",
            }
        ],
        at(1, 10),
    )
    s.tick(at(2, 3))
    b = bars(s)
    assert (b.at[1, "close"], b.at[1, "high"], b.at[1, "volume"]) == (501.0, 501.0, 100)


def test_cancel_of_a_finalized_trade_adjusts_cumulative_state_forward():
    s = session()
    feed(s, msg(at(1, 5), 500.0, size=100, i=1))
    feed(s, msg(at(1, 6), 500.0, size=300, i=2))
    s.tick(at(2, 3))
    s.on_messages(
        [{"T": "x", "S": "SPY", "x": "P", "i": 2, "a": "C", "t": at(2, 10).isoformat()}],
        at(2, 10),
    )
    s.tick(at(3, 3))
    b = bars(s)
    assert b.at[1, "cum_total_volume"] == 400  # never rewritten
    assert b.at[2, "cum_total_volume"] == 100 and b.at[2, "cum_vwap_volume"] == 100


def test_unknown_condition_discovered_late_marks_the_next_slot():
    s = session()
    feed(s, msg(at(1, 5), 500.0))
    feed(s, msg(at(1, 30), 500.0, c=(" ", "Y")), received=at(2, 5))
    feed(s, msg(at(2, 30), 500.0))
    s.tick(at(3, 3))
    b = bars(s)
    assert b.at[1, "interval_status"] == "PRESENT"  # earlier decisions stand
    assert b.at[2, "interval_status"] == "DATA_MISSING_OR_UNCERTAIN"
    assert "late_unknown_condition" in b.at[2, "flags"]


def test_premarket_silence_is_not_stale():
    s = session(stale=timedelta(seconds=5))
    feed(s, msg(pd.Timestamp(OPEN) - pd.Timedelta(hours=2), 498.0, c=("T",)))
    s.tick(pd.Timestamp(OPEN) - pd.Timedelta(minutes=30))  # 90 quiet minutes before the open
    assert s.health.unhealthy[-1][1] is not None


def test_premarket_levels_need_a_healthy_feed_from_0400():
    s = session()
    feed(s, msg(pd.Timestamp(OPEN) - pd.Timedelta(hours=2), 498.0, c=("T",)))
    feed(s, msg(pd.Timestamp(OPEN) - pd.Timedelta(hours=1), 499.0, c=("T",)))
    s.tick(at(2, 3))
    assert (s.premarket_levels.high, s.premarket_levels.low) == (499.0, 498.0)
    late = session(connect=pd.Timestamp(OPEN) - pd.Timedelta(hours=1))
    late.tick(at(2, 3))
    assert late.premarket_levels.high is None
    assert "premarket_feed_uncertain" in late.premarket_levels.flags


def test_idle_ticks_finalize_every_slot_through_the_close():
    s = session()
    s.tick(pd.Timestamp(DAY.close_utc) + pd.Timedelta(seconds=2))
    assert s.done and list(bars(s).index) == [1, 2, 3, 4, 5, 6]


def test_feed_guard():
    check_feed("sip", "shadow")
    check_feed("sip", "paper")
    check_feed("test", "test")
    for feed, mode in (("test", "shadow"), ("test", "paper"), ("sip", "test")):
        with pytest.raises(FeedGuardError):
            check_feed(feed, mode)
    with pytest.raises(FeedGuardError):
        check_feed("iex", "paper")
    assert stock_url("sip").endswith("/v2/sip") and stock_url("test").endswith("/v2/test")
