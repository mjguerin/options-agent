"""Replay harness and reference positions (SD-10, SD-11; acceptance SH-19, SH-23, SH-25, SH-26,
SH-29 to SH-31, SH-34 to SH-36, SH-40; VMR-X6 mechanics)."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tournament.features.engine import FeatureConfig, PriorContext, compute_session_features
from tournament.features.pipeline import SessionOutput
from tournament.marketdata.bars import DATA_MISSING_OR_UNCERTAIN, KNOWN_NO_ELIGIBLE_TRADES, PRESENT
from tournament.marketdata.calendar import SessionDay
from tournament.replay.harness import ContractViolation, ReplayRun
from tournament.replay.refpos import ReferencePosition
from tournament.strategies.base import CandidateSpec, Decision, Outcome, StrategyWorker, Transition

FCFG = FeatureConfig.load()
OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)


def session(close, status=None, n=None, day=date(2024, 6, 3)):
    n = n or len(close)
    close = np.asarray(close, float)
    status = status or [PRESENT] * n
    bars = pd.DataFrame(
        {
            "slot": np.arange(1, n + 1),
            "bar_end": [OPEN + timedelta(minutes=i + 1) for i in range(n)],
            "interval_status": status,
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": np.full(n, 1000),
            "session_vwap": close,
            "vwap_uncertain": np.maximum.accumulate(np.array(status) == DATA_MISSING_OR_UNCERTAIN),
        }
    )
    priced = bars["interval_status"] == PRESENT
    for c in ("open", "high", "low", "close"):
        bars.loc[~priced, c] = np.nan
    f, lv = compute_session_features(
        bars, f"XNYS-{day}", PriorContext(0.2, 501, 499, 500), None, None, np.full(n, 500.0), FCFG
    )
    sd = SessionDay(day, OPEN, OPEN + timedelta(minutes=n))
    return SessionOutput(sd, f"XNYS-{day}", bars, f, lv, ())


def spec(
    entry=500.0, inv=499.0, target=503.0, direction="bullish", timers=None, trail=None, ordinal=1
):
    return CandidateSpec(
        direction,
        entry,
        entry,
        inv,
        {"type": "fixed_level", "price": target},
        trail or {"type": "none"},
        timers or {},
        {},
        ("TEST",),
        f"setup-{ordinal}",
        ordinal,
    )


class Scripted(StrategyWorker):
    """Emits spec_at[slot] when allowed; records exits; reports state changes only on change."""

    strategy_id, version = "scripted", "test-1"

    def __init__(self, spec_at, force=False):
        super().__init__({"spec_at": sorted(spec_at)})
        self.spec_at, self.force, self.exits, self.contexts = spec_at, force, [], []

    def reset_session(self, session_id):
        self.state = "IDLE"

    def evaluate(self, ctx):
        self.contexts.append(ctx)
        s = self.spec_at.get(ctx.slot)
        if s is not None and (ctx.may_emit or self.force):
            return Decision(Outcome.CANDIDATE, "IDLE", setup_id=s.setup_id, candidate=s)
        if not ctx.may_emit:
            return Decision(Outcome.INELIGIBLE, self.state, tuple(b.value for b in ctx.blocks))
        if ctx.slot == 20 and self.state == "IDLE":
            self.state = "WATCHING_X"
            return Decision(
                Outcome.WATCHING,
                self.state,
                transitions=(Transition("IDLE", "WATCHING_X", Outcome.WATCHING, ("START",), "s1"),),
            )
        return Decision(Outcome.WATCHING, self.state)

    def on_reference_exit(self, event):
        self.exits.append(event)


def run(sessions, worker, run_id=None):
    return ReplayRun([worker], run_id=run_id, dataset_id="test").run_outputs(sessions)


FLAT = [500.0] * 390


def test_floor_last_entry_and_one_evaluation_per_slot():
    w = Scripted({10: spec(), 361: spec(ordinal=2)})
    r = run([session(FLAT)], w)
    assert len(r.evaluations) == 390 and r.candidates.empty
    ev = r.evaluations.set_index("slot")
    assert (
        "BEFORE_FLOOR" in ev.loc[10, "eligibility_blocks"]
        and ev.loc[15, "eligibility_blocks"] == []
    )
    assert (
        "AFTER_LAST_ENTRY" in ev.loc[361, "eligibility_blocks"]
        and ev.loc[360, "eligibility_blocks"] == []
    )


def test_emitting_while_blocked_is_a_contract_violation():
    with pytest.raises(ContractViolation):
        run([session(FLAT)], Scripted({10: spec()}, force=True))


def test_bad_geometry_is_a_contract_violation():
    with pytest.raises(ContractViolation):
        run([session(FLAT)], Scripted({30: spec(target=499.5)}))


def test_cutoff_exit_and_early_close_slots():  # SH-31, SH-18
    r = run([session(FLAT)], Scripted({300: spec(inv=490, target=510)}))
    assert r.reference_positions.iloc[0]["exit_slot"] == 380
    assert r.reference_positions.iloc[0]["exit_primary_reason"] == "SESSION_CUTOFF"
    early = run(
        [session([500.0] * 210)], Scripted({180: spec(inv=490, target=510), 181: spec(ordinal=2)})
    )
    assert early.reference_positions.iloc[0]["exit_slot"] == 200 and len(early.candidates) == 1


def test_cooldown_and_ref_position_block():  # SH-19
    close = FLAT.copy()
    close[100] = 498.0  # slot 101 closes below the invalidation
    w = Scripted({100: spec(), 101: spec(ordinal=2), 106: spec(ordinal=3), 107: spec(ordinal=4)})
    r = run([session(close)], w)
    ev = r.evaluations.set_index("slot")
    assert r.reference_positions.iloc[0]["exit_slot"] == 101
    assert "REF_POSITION_OPEN" in ev.loc[101, "eligibility_blocks"]
    assert all("COOLDOWN" in ev.loc[s, "eligibility_blocks"] for s in range(102, 107))
    assert list(r.candidates["trigger_slot"]) == [100, 107]


def test_reference_exit_event_and_transition_cause():  # SH-26 analog
    close = FLAT.copy()
    close[100] = 498.0
    w = Scripted({100: spec()})
    r = run([session(close)], w)
    assert w.exits[0].primary_reason == "INVALIDATION" and w.exits[0].slot == 101
    t = r.transitions[r.transitions["cause_type"] == "reference_exit"].iloc[0]
    assert pd.isna(t["snapshot_id"]) and t["from_state"] == "IN_POSITION"


def test_transitions_only_on_change():  # SH-25
    r = run([session(FLAT)], Scripted({}))
    assert len(r.evaluations) == 390 and len(r.transitions) == 1


def test_candidate_clock_fields():  # SH-36, D-37
    r = run([session(FLAT)], Scripted({50: spec()}))
    c = r.candidates.iloc[0]
    assert c["signal_bar_end"] == pd.Timestamp(OPEN + timedelta(minutes=50))
    assert c["expires_at"] - c["signal_bar_end"] == pd.Timedelta(seconds=62)
    assert bool(c["submission_eligible"])


def test_signal_keys_stable_across_runs_ids_not():  # SH-23
    a = run([session(FLAT)], Scripted({50: spec()}), run_id="run-a")
    b = run([session(FLAT)], Scripted({50: spec()}), run_id="run-b")
    assert a.candidates["signal_key"].tolist() == b.candidates["signal_key"].tolist()
    assert a.candidates["candidate_id"].tolist() != b.candidates["candidate_id"].tolist()
    cols = ["slot", "outcome", "setup_state", "eligibility_blocks", "snapshot_id"]
    assert a.evaluations[cols].astype(str).equals(b.evaluations[cols].astype(str))


def test_data_missing_slot_cancels():
    status = [PRESENT] * 390
    status[49] = DATA_MISSING_OR_UNCERTAIN
    w = Scripted({50: spec()})
    r = run([session(FLAT, status=status)], w)
    assert w.contexts[49].cancel_reason == DATA_MISSING_OR_UNCERTAIN and r.candidates.empty
    assert (
        "CANCEL_DATA_MISSING_OR_UNCERTAIN"
        in r.evaluations.set_index("slot").loc[50, "eligibility_blocks"]
    )


def test_workers_see_only_past_slots():
    w = Scripted({})
    run([session(FLAT)], w)
    assert all(
        len(c.history) == c.slot and c.history["slot"].iloc[-1] == c.slot for c in w.contexts
    )


# ---- reference-position exits, directly --------------------------------------------------


def row(slot, close, status=PRESENT, swings=None):
    return pd.Series(
        {
            "slot": slot,
            "close": close,
            "interval_status": status,
            "swings_new": swings or [],
            "bar_end": pd.Timestamp(OPEN + timedelta(minutes=slot)),
        }
    )


def ref(s):
    return ReferencePosition(
        "r",
        "c",
        s,
        100,
        pd.Timestamp(OPEN),
        (
            "SESSION_CUTOFF",
            "INVALIDATION",
            "TARGET",
            "TRAIL",
            "FOLLOW_THROUGH",
            "STAGNATION",
            "MAX_HOLD",
        ),
    )


def test_simultaneous_reasons_primary_and_triggered():  # SH-29, VMR-X6 mechanics
    p = ref(spec(timers={"max_hold": 2}))
    assert not p.evaluate(101, row(101, 500.2), 380)
    assert p.evaluate(102, row(102, 498.5), 380)
    assert p.primary_reason == "INVALIDATION" and p.triggered_reasons == (
        "INVALIDATION",
        "MAX_HOLD",
    )


def test_follow_through_final_slot_counts():  # SH-35
    p = ref(spec(timers={"ft_bars": 3, "ft_r": 0.5}))
    for s, c in ((101, 500.1), (102, 500.2)):
        assert not p.evaluate(s, row(s, c), 380)
    assert not p.evaluate(103, row(103, 500.5), 380)  # reached on the deadline slot: no exit
    q = ref(spec(timers={"ft_bars": 3, "ft_r": 0.5}))
    for s, c in ((101, 500.1), (102, 500.2)):
        q.evaluate(s, row(s, c), 380)
    assert q.evaluate(103, row(103, 500.4), 380) and q.primary_reason == "FOLLOW_THROUGH"


def test_follow_through_missing_deadline_close_fails():  # D-21: a gap can't establish progress
    p = ref(spec(timers={"ft_bars": 3, "ft_r": 0.5}))
    for s, c in ((101, 500.1), (102, 500.2)):
        assert not p.evaluate(s, row(s, c), 380)
    assert p.evaluate(103, row(103, None, DATA_MISSING_OR_UNCERTAIN), 380)
    assert p.primary_reason == "FOLLOW_THROUGH" and p.exit_price_stale


def test_stagnation_counts_slots_without_new_best_close():
    p = ref(spec(timers={"stag_bars": 3}))
    p.evaluate(101, row(101, 500.3), 380)  # new best
    assert not p.evaluate(102, row(102, 500.2), 380)
    assert not p.evaluate(103, row(103, 500.3), 380)  # equal isn't new
    assert p.evaluate(104, row(104, 500.1), 380) and p.primary_reason == "STAGNATION"


def test_stale_exit_price_on_no_close_slot():  # SH-34
    p = ref(spec(timers={"max_hold": 2}))
    p.evaluate(101, row(101, 500.4), 380)
    assert p.evaluate(102, row(102, np.nan, status=KNOWN_NO_ELIGIBLE_TRADES), 380)
    assert (p.primary_reason, p.exit_price, p.exit_price_stale) == ("MAX_HOLD", 500.4, True)


def test_trail_activates_ratchets_and_never_loosens():  # SH-30
    p = ref(spec(trail={"type": "swing_trail", "activate_r": 1.0, "buffer": 0.25}))

    def sw(slot, price):
        return [{"side": "low", "pivot_slot": slot, "price": price, "confirmed_slot": slot + 2}]

    p.evaluate(101, row(101, 500.5), 380)
    p.evaluate(102, row(102, 501.0, swings=sw(101, 500.4)), 380)  # activates at +1R; stop 500.15
    assert p.trail_active and p.trail_stop == pytest.approx(500.15)
    p.evaluate(103, row(103, 501.2, swings=sw(102, 500.8)), 380)  # higher swing: stop 500.55
    p.evaluate(104, row(104, 501.3, swings=sw(103, 500.3)), 380)  # lower swing: unchanged
    assert p.trail_stop == pytest.approx(500.55)
    assert p.evaluate(105, row(105, 500.5), 380) and p.primary_reason == "TRAIL"


def test_bearish_mirror():
    p = ref(
        spec(entry=500.0, inv=501.0, target=497.0, direction="bearish", timers={"max_hold": 50})
    )
    assert not p.evaluate(101, row(101, 499.0), 380)
    assert p.evaluate(102, row(102, 496.9), 380) and p.primary_reason == "TARGET"
