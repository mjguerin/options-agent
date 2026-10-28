"""The load meters: where the live loop spends its time (measuring aid, never behaviour)."""

import asyncio
import time

import msgpack
import pandas as pd
import pytest

from tournament.live.load import LoadMeter, LoopProbe, write_load
from tournament.live.option_stream import AlpacaOptionStream


def at(s: float) -> pd.Timestamp:
    return pd.Timestamp("2026-10-28T15:00:00Z") + pd.Timedelta(seconds=s)


def test_the_meter_totals_each_second_apart():
    m = LoadMeter("option")
    m.waited(at(0.1), 0.4)
    m.frame(at(0.5), 200, 3, 0.002, 1.5)
    m.frame(at(0.7), 100, 1, 0.010, 4.0)
    m.frame(at(1.2), 50, 2, 0.001, None)
    m.finish()
    first, second = m.rows
    assert (first["second"], first["frames"], first["msgs"], first["bytes"]) == (
        int(at(0).timestamp()), 2, 4, 300,
    )  # fmt: skip
    assert first["busy_s"] == pytest.approx(0.012) and first["wait_s"] == pytest.approx(0.4)
    assert first["max_frame_ms"] == pytest.approx(10.0) and first["max_lag_s"] == 4.0
    assert second["frames"] == 1 and second["max_lag_s"] is None  # no lag known for that frame


def test_the_option_stream_reports_how_late_its_newest_quote_was():
    s = AlpacaOptionStream(
        "opra", "k", "s", mode="shadow", max_symbols=10, tick_s=1.0, on_messages=lambda *a: None,
        on_tick=lambda n: None, on_connected=lambda a: None, on_disconnected=lambda a: None,
        backoff_s=(1,), ping_interval_s=10, handshake_timeout_s=5,
    )  # fmt: skip
    received = at(10)
    quote_time = received.value - 3_000_000_000  # stamped 3 s before it arrived
    msgs = [{"T": "q", "t": quote_time - 1_000_000_000}, {"T": "q", "t": quote_time}]
    assert s.message_lag_s(msgs, received) == pytest.approx(3.0)
    assert s.message_lag_s([{"T": "subscription"}], received) is None
    assert s.meter.name == "option"


def test_the_read_loop_feeds_the_meter_without_changing_what_it_delivers():
    from tournament.live.quotes import OptionFeed

    feed = OptionFeed(
        pd.Timedelta(seconds=5), pd.Timedelta(seconds=2), pd.Timedelta(seconds=10), 100,
        pd.Timedelta(seconds=1), 50,
    )  # fmt: skip

    class WS:
        def __init__(self):
            t = pd.Timestamp.now(tz="UTC").value
            q = {
                "T": "q",
                "S": "SPY261028C00770000",
                "t": t,
                "bp": 1.0,
                "bs": 1,
                "ap": 1.1,
                "as": 1,
            }
            self.frames = [msgpack.packb([q, q]), msgpack.packb([q])]

        async def recv(self):
            if not self.frames:
                await asyncio.sleep(3600)
            return self.frames.pop(0)

        async def send(self, x):
            pass

    s = AlpacaOptionStream(
        "opra", "k", "s", mode="shadow", max_symbols=10, tick_s=0.05, on_messages=feed.on_messages,
        on_tick=lambda n: None, on_connected=lambda a: None, on_disconnected=lambda a: None,
        backoff_s=(1,), ping_interval_s=10, handshake_timeout_s=5,
    )  # fmt: skip

    async def go():
        until = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=0.4)
        await s._pump(WS(), until)

    asyncio.run(go())
    s.meter.finish()
    assert feed.book.counts["quotes"] == 3  # all three quotes got through
    assert sum(r["frames"] for r in s.meter.rows) == 2 and sum(r["msgs"] for r in s.meter.rows) == 3
    assert all(
        r["max_lag_s"] is not None and r["max_lag_s"] < 5 for r in s.meter.rows if r["frames"]
    )


def test_the_probe_sees_a_blocked_event_loop():
    probe = LoopProbe(0.02)

    async def go():
        clock = lambda: pd.Timestamp.now(tz="UTC")  # noqa: E731
        until = clock() + pd.Timedelta(seconds=2.3)

        async def blocker():
            await asyncio.sleep(0.2)
            time.sleep(0.4)  # something synchronous holds the loop for 400 ms

        await asyncio.gather(probe.run(until, clock), blocker())

    asyncio.run(go())
    assert max(r["loop_lag_max_ms"] for r in probe.rows) >= 300
    assert min(r["loop_lag_max_ms"] for r in probe.rows) < 100  # and the other seconds are quiet


def test_the_summary_and_files_are_written(tmp_path):
    m = LoadMeter("option")
    m.frame(at(0.5), 100, 4, 0.2, 7.0)
    m.frame(at(1.5), 100, 9, 0.3, 1.0)
    probe = LoopProbe(0.1)
    probe.rows = [{"second": 1, "loop_lag_max_ms": 1500.0, "loop_lag_mean_ms": 2.0}]
    summary = write_load(tmp_path, [m], probe)
    assert (tmp_path / "feed_load.parquet").exists() and (tmp_path / "loop_load.parquet").exists()
    o = summary["option"]
    assert o["msgs"] == 13 and o["busiest_second_msgs"] == 9 and o["worst_lag_s"] == 7.0
    assert summary["loop"] == {
        "worst_block_ms": 1500.0, "seconds_blocked_over_250ms": 1, "seconds_blocked_over_1s": 1,
    }  # fmt: skip
