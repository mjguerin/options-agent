"""Shadow accounts: the harness hooks feed candidates and reference exits to five modeled
accounts that trade the live option quotes. Replay without hooks is unchanged."""

from types import SimpleNamespace

import pandas as pd
from test_account_runtime import CALL, T0, Market, at, chain, prices
from test_replay import FLAT, Scripted, session, spec

from tournament.live.shadow_accounts import ShadowAccounts
from tournament.marketdata.calendar import SessionDay
from tournament.replay.harness import ReplayRun

SIDS = ["vpc", "orb", "brt", "fbr", "vmr"]


def build(tmp_path):
    m = Market()
    collector = SimpleNamespace(feed=m.feed, universe=SimpleNamespace(contracts=chain()))
    day = SessionDay(
        pd.Timestamp("2026-10-26").date(), at(0).to_pydatetime(), at(23 * 60 + 600).to_pydatetime()
    )
    acc = ShadowAccounts(day, collector, SIDS, 10_000.0, 10, tmp_path / "a.sqlite", 1.0, 300.0)
    return acc, m


def record(cid, sid, direction="bullish", available=0.0):
    return {
        "candidate_id": cid,
        "account_binding": f"acct:{sid}",
        "underlying_direction": direction,
        "entry_reference": 765.3,
        "submission_eligible": True,
        "expires_at": at(available + 62),
        "signal_available_at": at(available),
    }


def test_five_accounts_trade_independently_from_one_quote_feed(tmp_path):
    acc, m = build(tmp_path)
    prices(m, at(0))
    acc._candidate(record("u", "vpc", "bullish"))
    acc._candidate(record("d", "vmr", "bearish"))  # opposing candidates, simultaneous (AC-01)
    for s in (1.5, 2.5):
        prices(m, at(s))
        acc.tick(at(s))
    s = acc.summary()
    assert s["errors"] == []
    assert s["accounts"]["acct:vpc"]["open_contracts"] == {CALL: 13}
    assert list(s["accounts"]["acct:vmr"]["open_contracts"]) == [CALL.replace("C0", "P0")]
    assert s["accounts"]["acct:orb"]["decisions"] == 0
    assert s["accounts"]["acct:vpc"]["outcomes"] == {"ACCEPTED": 1}


def test_a_reference_exit_reaches_only_its_own_account(tmp_path):
    acc, m = build(tmp_path)
    prices(m, at(0))
    acc._candidate(record("u", "vpc"))
    acc._candidate(record("d", "vmr", "bearish"))
    for s in (1.5, 2.5):
        prices(m, at(s))
        acc.tick(at(s))
    acc._reference_exit("vpc", "u", "TARGET", at(3))
    vpc = acc.router.runtimes["acct:vpc"].exits_requested
    vmr = acc.router.runtimes["acct:vmr"].exits_requested
    assert [r for _, _, r in vpc] == ["REF_TARGET"] and vmr == []


def test_a_failing_account_is_recorded_and_never_stops_the_others(tmp_path):
    acc, m = build(tmp_path)
    prices(m, at(0))
    bad = record("x", "vpc")
    del bad["entry_reference"]  # a malformed candidate
    acc._candidate(bad)
    acc._candidate(record("d", "vmr", "bearish"))
    assert len(acc.errors) == 1 and "entry_reference" in acc.errors[0]
    assert acc.router.runtimes["acct:vmr"].decisions[0].accepted


def test_the_position_loop_runs_at_most_once_per_interval(tmp_path):
    acc, m = build(tmp_path)
    calls = []
    for rt in acc.router.runtimes.values():
        rt.on_tick = calls.append
    acc.tick(at(1.0))
    acc.tick(at(1.4))
    acc.tick(at(2.1))
    assert len(calls) == 2 * len(SIDS)


def test_the_harness_calls_the_hooks_and_replay_without_hooks_is_unchanged():
    seen, exits = [], []
    w = Scripted({300: spec(inv=490, target=510)})
    r = ReplayRun([w], run_id="t", dataset_id="t")
    r.on_candidate.append(seen.append)
    r.on_reference_exit.append(lambda *a: exits.append(a))
    res = r.run_outputs([session(FLAT)])
    (rec,) = seen
    assert rec["account_binding"] == "acct:scripted" and rec["candidate_id"] in set(
        res.candidates["candidate_id"]
    )
    assert rec["signal_available_at"] < rec["expires_at"]
    assert [(e[0], e[2]) for e in exits] == [("scripted", "SESSION_CUTOFF")]
    plain = ReplayRun([Scripted({300: spec(inv=490, target=510)})], run_id="t", dataset_id="t")
    assert len(plain.run_outputs([session(FLAT)]).candidates) == len(res.candidates) == 1
    assert T0  # the shared fixtures module is imported for its helpers


def test_books_are_compared_with_the_broker_periodically_and_a_difference_blocks_entries(tmp_path):
    acc, m = build(tmp_path)
    prices(m, at(0))
    acc._candidate(record("u", "vpc"))
    for s in (1.5, 2.5):
        prices(m, at(s))
        acc.tick(at(s))
    vpc = acc.router.runtimes["acct:vpc"]
    assert vpc.orders.entries_blocked is None
    sim = acc.brokers["acct:vpc"]
    next(iter(sim.orders.values()))["filled"] = 9  # the modeled broker now disagrees
    acc.tick(at(10.0))  # not yet due: the first comparison was at t = 1.5
    assert vpc.orders.entries_blocked is None
    acc.tick(at(310.0))
    assert vpc.orders.entries_blocked and "position mismatch" in vpc.orders.entries_blocked
