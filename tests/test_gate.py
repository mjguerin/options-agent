"""Step 2 gate checks: determinism comparison, traceability, calendar session length."""

import json

from strategy_fixtures import bar, frame, levels, run, swing

from tournament.replay.gate import check_traceability, compare_runs
from tournament.strategies.base import Decision, Outcome, StrategyWorker
from tournament.strategies.vmr import VMR


def vmr_session():
    df = frame(price=500.0, vwap=500.0, regime="RANGE")
    bar(df, 99, 505.85, 505.70, 505.80)
    bar(df, 100, 506.15, 506.00, 506.10)
    bar(df, 101, 506.50, 506.00, 506.20)
    bar(df, 102, 506.30, 505.95, 506.00)
    bar(df, 103, 506.10, 505.80, 505.90)
    swing(df, "high", 101, 506.50)
    return df


def test_identical_runs_compare_equal_and_differences_are_caught():
    a = run(VMR(), vmr_session(), run_id="a")
    b = run(VMR(), vmr_session(), run_id="b")
    assert compare_runs(a, b).passed
    b.evaluations.loc[b.evaluations.index[200], "outcome"] = "CANDIDATE"
    res = compare_runs(a, b)
    assert not res.passed and res.failures[0]["table"] == "evaluations"


def test_missing_values_compare_equal_whatever_the_column_dtype():
    """A truncated run's all-None column holds None; the full run's holds NaN."""
    a = run(VMR(), vmr_session(), run_id="a")
    b = run(VMR(), vmr_session(), run_id="b")
    a.evaluations["setup_id"] = (
        a.evaluations["setup_id"].astype(object).where(a.evaluations["setup_id"].notna(), None)
    )
    b.evaluations["setup_id"] = (
        b.evaluations["setup_id"]
        .astype(object)
        .where(b.evaluations["setup_id"].notna(), float("nan"))
    )
    assert compare_runs(a, b).passed


def test_traceability_passes_and_detects_tampering():
    r = run(VMR(), vmr_session())
    assert len(r.candidates) == 1 and check_traceability(r).passed
    i = r.snapshots.index[102]  # tamper with the trigger slot's stored identity
    ident = json.loads(r.snapshots.at[i, "identity"])
    ident["values"]["close"] = 1.0
    r.snapshots.at[i, "identity"] = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    kinds = {f["kind"] for f in check_traceability(r).failures}
    assert "snapshot_hash_mismatch" in kinds and "candidate_snapshot_mismatch" in kinds


def test_candidate_entry_must_equal_its_snapshot_close():
    r = run(VMR(), vmr_session())
    r.candidates.loc[r.candidates.index[0], "entry_reference"] = 505.0
    assert {f["kind"] for f in check_traceability(r).failures} == {"candidate_snapshot_mismatch"}


class Probe(StrategyWorker):
    strategy_id, version = "probe", "t"

    def __init__(self):
        super().__init__({})
        self.seen = []

    def reset_session(self, session_id):
        pass

    def evaluate(self, ctx):
        self.seen.append((ctx.n_slots, ctx.last_entry_slot, ctx.cutoff_slot))
        return Decision(Outcome.WATCHING, "IDLE")


def test_session_length_comes_from_the_calendar_not_available_rows():
    """Regression: with only 200 bars so far, last entry and cutoff must still be 360 and 380."""
    from datetime import timedelta

    from strategy_fixtures import DAY, OPEN, SID

    from tournament.features.pipeline import SessionOutput
    from tournament.marketdata.calendar import SessionDay
    from tournament.replay.harness import ReplayRun

    df = frame().iloc[:200]
    out = SessionOutput(
        SessionDay(DAY, OPEN, OPEN + timedelta(minutes=390)), SID, None, df, levels([]), ()
    )
    w = Probe()
    ReplayRun([w], run_id="t", dataset_id="t").run_outputs([out])
    assert set(w.seen) == {(390, 360, 380)} and len(w.seen) == 200


def test_every_slot_has_one_snapshot_shared_by_workers():
    from datetime import timedelta

    from strategy_fixtures import DAY, OPEN, SID

    from tournament.features.pipeline import SessionOutput
    from tournament.marketdata.calendar import SessionDay
    from tournament.replay.harness import ReplayRun
    from tournament.strategies.orb import ORB

    out = SessionOutput(
        SessionDay(DAY, OPEN, OPEN + timedelta(minutes=390)), SID, None, frame(), levels([]), ()
    )
    r = ReplayRun([VMR(), ORB()], run_id="t", dataset_id="t").run_outputs([out])
    assert len(r.snapshots) == 390
    assert r.evaluations["snapshot_id"].isin(r.snapshots["snapshot_id"]).all()
