"""Shadow engine: one shared snapshot per slot, errors never stop the feed, late snapshots."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
from test_replay import Scripted, spec

from tournament.features.engine import PriorContext
from tournament.live.engine import ShadowEngine
from tournament.live.session import LiveSession
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.strategies.base import Decision, Outcome, StrategyWorker

OPEN = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
DAY = SessionDay(date(2026, 10, 26), OPEN, OPEN + timedelta(minutes=390))
CFG = EligibilityConfig()


def at(slot: int, sec: float) -> pd.Timestamp:
    return pd.Timestamp(OPEN + timedelta(minutes=slot - 1, seconds=sec))


def trade(slot: int, price: float, i: int) -> dict:
    return {
        "T": "t",
        "S": "SPY",
        "t": at(slot, 5).isoformat(),
        "p": price,
        "s": 100,
        "x": "P",
        "c": [" "],
        "i": i,
        "z": "B",
    }


def start(workers):
    s = LiveSession(
        DAY,
        CFG,
        finalize_delay=timedelta(seconds=2),
        halt_codes=frozenset({"H", "P"}),
        resume_codes=frozenset({"T"}),
        stale_after=timedelta(hours=1),
    )
    eng = ShadowEngine(s, workers, PriorContext(0.2, 501, 499, 500), None, usage=Usage.LIVE)
    s.on_connected(pd.Timestamp(DAY.extended_start_utc) - pd.Timedelta(minutes=1))
    return s, eng


def drive(s, upto: int, lag: float = 0.05):
    """One trade per slot, each received `lag` seconds after it printed."""
    for m in range(1, upto + 1):
        s.on_messages([trade(m, 500.0, m)], at(m, 5 + lag))
    s.tick(s.builder.bar_end(upto) + pd.Timedelta(seconds=2.1))


def test_every_finalized_slot_is_decided_once_with_one_shared_snapshot():
    a, b = Scripted({}), Scripted({})
    b.strategy_id = "scripted2"
    s, eng = start([a, b])
    drive(s, 40)
    r = eng.result()
    assert eng.error is None and eng.stepped == 40
    assert len(r.snapshots) == 40 and len(r.evaluations) == 80
    assert r.evaluations.groupby("slot")["snapshot_id"].nunique().eq(1).all()
    assert len(eng.latencies) == 40


def test_a_worker_failure_is_recorded_and_the_feed_keeps_recording():
    class Boom(StrategyWorker):
        strategy_id, version = "boom", "t"

        def __init__(self):
            super().__init__({})

        def reset_session(self, session_id):
            pass

        def evaluate(self, ctx):
            if ctx.slot == 10:
                raise RuntimeError("bug")
            return Decision(Outcome.WATCHING, "IDLE")

    s, eng = start([Boom()])
    drive(s, 20)
    assert eng.error is not None and eng.error.startswith("slot 10")
    assert len(s.bars) == 20  # bars kept coming after the engine stopped
    assert eng.stepped == 9


def candidate_session(late_by: float):
    w = Scripted({20: spec(inv=490, target=510)})
    s, eng = start([w])
    drive(s, 19)
    s.on_messages([trade(20, 500.0, 20)], at(20, 5.05))
    # nothing more arrives until `late_by` seconds after slot 20's bar end
    s.on_messages([trade(21, 500.0, 21)], s.builder.bar_end(20) + pd.Timedelta(seconds=late_by))
    return eng.result().candidates


def test_an_on_time_snapshot_gives_a_submission_eligible_candidate():
    c = candidate_session(late_by=3.0)
    assert len(c) == 1 and bool(c.iloc[0]["submission_eligible"]) is True


def test_a_candidate_from_a_late_snapshot_is_not_submission_eligible():  # D-37, SD-01
    c = candidate_session(late_by=75.0)  # finalized 75 s after bar end; expiry is at 62 s
    assert len(c) == 1  # strategy state never depends on processing time
    assert bool(c.iloc[0]["submission_eligible"]) is False
    assert c.iloc[0]["signal_available_at"] > c.iloc[0]["expires_at"]
