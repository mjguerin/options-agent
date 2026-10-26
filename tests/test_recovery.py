"""Restart recovery (D-67): a crash must not erase positions, orders, halts, strategy state or
candidate identities. The strategy side is rebuilt by replaying the journal; the execution side
comes back from the durable account database."""

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from test_account_runtime import CALL, Market, build, cand, prices
from test_account_runtime import at as t_at
from test_replay import Scripted, spec

from tournament.features.engine import PriorContext
from tournament.live.engine import ShadowEngine
from tournament.live.recovery import mark_outage_from, previous_runs, read_journal, replay_journals
from tournament.live.session import LiveSession
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.orders.model import Purpose, Status
from tournament.orders.store import IntentStore
from tournament.replay.gate import compare_runs

OPEN = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
DAY = SessionDay(date(2026, 10, 26), OPEN, OPEN + timedelta(minutes=60))
CFG = EligibilityConfig()


def at(slot: int, sec: float = 0.0) -> pd.Timestamp:
    return pd.Timestamp(OPEN + timedelta(minutes=slot - 1, seconds=sec))


def trade(slot: int, price: float, i: int) -> dict:
    return {"T": "t", "S": "SPY", "t": at(slot, 5).isoformat(), "p": price, "s": 100,
            "x": "P", "c": [" "], "i": i, "z": "B"}  # fmt: skip


def new_process():
    """A fresh process: a session, and a decision engine over scripted strategies."""
    s = LiveSession(
        DAY, CFG, finalize_delay=timedelta(seconds=2), stale_after=timedelta(hours=1),
        halt_codes=frozenset({"H"}), resume_codes=frozenset({"T"}),
        informational_codes=frozenset({"Q"}),
        unknown_status_hold=timedelta(seconds=60),
    )  # fmt: skip
    w = Scripted({20: spec(inv=490, target=510), 22: spec(inv=490, target=510, ordinal=2)})
    eng = ShadowEngine(
        s, [w], PriorContext(0.2, 501, 499, 500), None, usage=Usage.LIVE, run_id="live-test"
    )
    return s, eng


class Journal:
    """What the runner writes: every frame with its receive time, and connection events."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a")

    def frame(self, msgs, received):
        self.f.write(json.dumps({"r": received.isoformat(), "m": msgs}) + "\n")
        self.f.flush()

    def event(self, kind, when):
        self.f.write(json.dumps({"r": when.isoformat(), "event": kind}) + "\n")
        self.f.flush()


def feed(session, journal, slots, start_id=0):
    """One trade per slot, ticking just before each frame like the live loop does."""
    for k, slot in enumerate(slots):
        received = at(slot, 5.05)
        session.tick(received - pd.Timedelta(milliseconds=1))
        msgs = [trade(slot, 500.0 + (slot % 3) * 0.1, start_id + k + 1)]
        if journal is not None:
            journal.frame(msgs, received)
        session.on_messages(msgs, received)


CRASH_AFTER, RECONNECT_AT = 24, at(27, 1.0)


def test_a_restart_rebuilds_the_strategy_state_exactly(tmp_path):
    # A: the same session without a restart, but with the same outage in the feed
    sa, ea = new_process()
    ja = Journal(tmp_path / "a" / "stream.jsonl")
    sa.on_connected(at(1, 0))
    ja.event("connected", at(1, 0))
    feed(sa, ja, range(1, CRASH_AFTER + 1))
    last_at = at(CRASH_AFTER, 5.05)
    sa.on_disconnected(last_at)
    sa.on_connected(RECONNECT_AT)
    feed(sa, ja, range(27, 46), start_id=100)
    sa.tick(at(61, 3))

    # B: process one runs until the crash; process two replays the journal and carries on
    s1, e1 = new_process()
    j1 = Journal(tmp_path / "day" / "shadow" / "stream.jsonl")
    s1.on_connected(at(1, 0))
    j1.event("connected", at(1, 0))
    feed(s1, j1, range(1, CRASH_AFTER + 1))
    del s1, e1  # the crash
    s2, e2 = new_process()
    runs = previous_runs(tmp_path / "day", "shadow")
    assert [r.name for r in runs] == ["shadow"]
    info = replay_journals(s2, runs)
    mark_outage_from(s2, info["last_at"])
    assert info["frames"] == CRASH_AFTER and info["last_at"] == last_at
    s2.on_connected(RECONNECT_AT)
    feed(s2, None, range(27, 46), start_id=100)
    s2.tick(at(61, 3))

    ra, rb = ea.result(), e2.result()
    check = compare_runs(ra, rb)
    assert check.passed, check.failures
    assert list(ra.candidates["candidate_id"]) == list(rb.candidates["candidate_id"])
    bars_a, bars_b = pd.DataFrame(sa.bars), pd.DataFrame(s2.bars)
    cols = ["slot", "interval_status", "close", "volume", "session_vwap", "vwap_uncertain"]
    pd.testing.assert_frame_equal(bars_a[cols], bars_b[cols])
    # the crash is a gap, never a quiet market
    uncertain = bars_b[bars_b["interval_status"] == "DATA_MISSING_OR_UNCERTAIN"]["slot"].tolist()
    assert uncertain[0] == CRASH_AFTER and 26 in uncertain
    assert bool(bars_b.loc[bars_b["slot"] == 40, "vwap_uncertain"].iloc[0]) is True  # stays sticky


def test_the_one_session_identity_gives_the_same_candidate_ids_after_a_restart(tmp_path):
    ids = []
    for _ in range(2):  # two separate processes, as after a restart
        s, e = new_process()
        s.on_connected(at(1, 0))
        feed(s, None, range(1, 24))
        ids.append(list(e.result().candidates["candidate_id"]))
    assert ids[0] == ids[1] and len(ids[0]) == 1


def test_journals_from_several_starts_replay_in_order_and_a_torn_line_is_skipped(tmp_path):
    day = tmp_path / "day"
    for name in ("shadow", "shadow-r2", "shadow-r10", "shadow-r3"):
        j = Journal(day / name / "stream.jsonl")
        j.event("connected", at(1, 0))
    assert [p.name for p in previous_runs(day, "shadow")] == [
        "shadow", "shadow-r2", "shadow-r3", "shadow-r10"
    ]  # fmt: skip
    path = day / "shadow" / "stream.jsonl"
    with path.open("a") as f:
        f.write('{"r": "2026-10-26T13:3')  # the process died mid-write
    kinds = [k for _, k, _ in read_journal(path)]
    assert kinds == ["connected"]
    assert previous_runs(tmp_path / "empty", "shadow") == []


# ---- the execution side ------------------------------------------------------------------------


def durable(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'accounts.sqlite'}")


def build_durable(tmp_path, market=None):
    from tournament.db import metadata

    engine = durable(tmp_path)
    metadata.create_all(engine)
    rt, sim, m = build(engine=engine, market=market)
    sim.restore(rt.store, rt.binding)
    rt.restore_state()
    return rt, sim, m


def hold(rt, m):
    prices(m, t_at(0))
    assert rt.on_candidate(t_at(0), cand()).accepted
    for s in (1.5, 2.5):
        prices(m, t_at(s))
        rt.on_tick(t_at(s))
    assert rt.orders.position(CALL) == 13


def test_a_restart_keeps_the_position_the_cash_the_claim_and_the_modeled_books(tmp_path):
    rt, sim, m = build_durable(tmp_path)
    hold(rt, m)
    cash, claim = sim.cash(), rt.ledger.exposure_usd(rt.binding)
    # the process dies; a new one opens the same files
    rt2, sim2, m2 = build_durable(tmp_path)
    assert rt2.orders.position(CALL) == 13
    assert sim2.cash() == pytest.approx(cash) and rt2.ledger.exposure_usd(rt2.binding) == claim
    assert [(p.symbol, p.qty) for p in sim2.positions()] == [(CALL, 13)]
    assert rt2.orders.reconcile_positions(t_at(5)) == []  # our books and the broker's agree
    # the new process manages the inherited position: a stop works against the restored books
    prices(m2, t_at(10), call=(1.15, 1.16))
    rt2.on_tick(t_at(10))
    for s in (11.5, 12.5):
        prices(m2, t_at(s), call=(1.15, 1.16))
        rt2.on_tick(t_at(s))
    assert rt2.orders.position(CALL) == 0 and [r for _, _, r in rt2.exits_requested] == [
        "PREMIUM_STOP"
    ]


def test_the_daily_limit_halt_the_pause_and_the_entry_block_survive_a_crash(tmp_path):
    rt, sim, m = build_durable(tmp_path)
    hold(rt, m)
    prices(m, t_at(10), call=(1.05, 1.06))  # an open loss past the daily limit
    rt.on_tick(t_at(10))
    rt.operator_pause(t_at(11))
    rt.orders.store.event(rt.binding, t_at(12), "entries_blocked", diffs=[["X", 1, 0]])
    assert rt.halted == "DAILY_LIMIT"
    rt2, _, _ = build_durable(tmp_path)
    assert rt2.halted == "DAILY_LIMIT" and rt2.operator_halt == "OPERATOR_PAUSE"
    assert rt2.orders.entries_blocked and "restored after restart" in rt2.orders.entries_blocked
    d = rt2.on_candidate(t_at(20), cand("c2", expires_at=t_at(100)))
    assert not d.accepted and d.reasons == ("DAILY_LIMIT",)


def test_a_resume_after_a_pause_is_remembered_in_order(tmp_path):
    rt, _, m = build_durable(tmp_path)
    prices(m, t_at(0))
    rt.operator_pause(t_at(1))
    rt.operator_resume(t_at(2))
    rt2, _, _ = build_durable(tmp_path)
    assert rt2.operator_halt is None
    rt.operator_pause(t_at(3))
    rt3, _, _ = build_durable(tmp_path)
    assert rt3.operator_halt == "OPERATOR_PAUSE"


def test_an_entry_order_in_flight_is_settled_on_restart_not_resent(tmp_path):
    rt, sim, m = build_durable(tmp_path)
    prices(m, t_at(0))
    d = rt.on_candidate(t_at(0), cand())
    assert d.intent.status is Status.OPEN
    rt2, sim2, m2 = build_durable(tmp_path)
    assert d.intent.client_order_id in sim2.orders  # the modeled broker still has it
    prices(m2, t_at(3))
    rt2.orders.recover(t_at(3))
    assert len([i for i in rt2.store.all_for(rt2.binding) if i.purpose is Purpose.ENTRY]) == 1
    rt2.on_tick(t_at(4))
    assert rt2.orders.position(CALL) == 13  # filled by the restored model, once


def test_a_position_that_outlived_its_reference_position_is_sold_on_recovery(tmp_path):
    from tournament.live.shadow_accounts import ShadowAccounts

    rt, sim, m = build_durable(tmp_path)
    hold(rt, m)
    rt2, sim2, m2 = build_durable(tmp_path)
    prices(m2, t_at(20))
    acc = ShadowAccounts.__new__(ShadowAccounts)  # recovery only needs the router and the store
    acc.router = type("R", (), {"runtimes": {rt2.binding: rt2}})()
    acc.store, acc.errors = rt2.store, []
    acc.recover(t_at(20), [("vpc", "c1", "TARGET")])  # the strategy side says c1 already exited
    assert [r for _, _, r in rt2.exits_requested] == ["REF_TARGET"] and acc.errors == []


def test_a_recovery_failure_halts_the_account_and_says_so(tmp_path):
    from tournament.live.shadow_accounts import ShadowAccounts

    rt, _, _ = build_durable(tmp_path)

    def boom(now):
        raise RuntimeError("cannot settle")

    rt.orders.recover = boom
    acc = ShadowAccounts.__new__(ShadowAccounts)
    acc.router = type("R", (), {"runtimes": {rt.binding: rt}})()
    acc.store, acc.errors = rt.store, []
    acc.recover(t_at(0), [])
    assert rt.operator_halt == "RECOVERY_FAILED" and "cannot settle" in acc.errors[0]


def test_the_post_close_report_asks_the_broker_and_never_assumes_flat(tmp_path):
    from tournament.live.shadow_accounts import ShadowAccounts

    rt, sim, m = build_durable(tmp_path)
    hold(rt, m)
    acc = ShadowAccounts.__new__(ShadowAccounts)
    acc.router = type("R", (), {"runtimes": {rt.binding: rt}})()
    acc.store, acc.errors = rt.store, []
    report = acc.post_close(t_at(30))[rt.binding]
    assert report["still_held"] == {CALL: 13} and report["flat"] is False
    sim.orders[next(iter(sim.orders))]["filled"] = 9  # the broker disagrees with our books
    report = acc.post_close(t_at(31))[rt.binding]
    assert report["position_mismatches"] and rt.orders.entries_blocked
    assert isinstance(Market(), Market) and IntentStore  # helpers imported for the fixtures
