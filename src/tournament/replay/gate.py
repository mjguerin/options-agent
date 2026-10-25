"""Step 2 gate checks (project plan Step 2; AC-03; BRD 6.1, 11).

- truncation: rebuild a session from trades cut off at slot m (trades -> bars -> features ->
  harness -> workers) and require every record for slots <= m to equal the full-session run.
  This is the whole-chain no-look-ahead test.
- determinism: two independent runs produce identical records, apart from run-scoped ids.
- traceability: every decision resolves to a stored snapshot whose hash re-verifies.

Each check returns a GateCheck; failures carry concrete examples.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from tournament.features.engine import FeatureConfig, compute_session_features
from tournament.features.pipeline import SessionOutput
from tournament.marketdata.bars import build_session_bars
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, RunResult
from tournament.strategies.base import StrategyWorker

# record columns compared across runs (run-scoped ids excluded)
EVAL_COLS = [
    "strategy_id",
    "session_id",
    "slot",
    "snapshot_id",
    "readiness_status",
    "outcome",
    "setup_state",
    "reason_codes",
    "eligibility_blocks",
    "setup_id",
]
TRANS_COLS = [
    "strategy_id",
    "session_id",
    "slot",
    "cause_type",
    "from_state",
    "to_state",
    "outcome",
    "reason_codes",
    "setup_id",
    "anchors",
]
CAND_COLS = [
    "signal_key",
    "strategy_id",
    "session_id",
    "trigger_slot",
    "underlying_direction",
    "entry_reference",
    "invalidation_level",
    "target",
    "trail",
    "timers",
    "features",
]
REF_COLS = [
    "strategy_id",
    "session_id",
    "entry_slot",
    "exit_slot",
    "reference_exit_price",
    "exit_primary_reason",
    "exit_triggered_reasons",
]


@dataclass
class GateCheck:
    name: str
    passed: bool
    checked: int
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "checked": self.checked,
            "failures": self.failures[:20],
            "failure_count": len(self.failures),
        }


def _cell(v) -> str:
    if hasattr(v, "tolist") and not isinstance(v, str):
        v = list(v)
    elif v is None or (isinstance(v, float) and v != v):
        v = None  # pandas stores a missing value as None or NaN depending on column dtype
    return json.dumps(v, default=str, sort_keys=True)


def _canon(df: pd.DataFrame, cols: list[str], sort: list[str]) -> list[tuple]:
    if df.empty:
        return []
    d = df[[c for c in cols if c in df.columns]].copy()
    for c in d.columns:
        d[c] = d[c].map(_cell)
    return sorted(
        map(tuple, d.sort_values([c for c in sort if c in d.columns]).to_numpy().tolist())
    )


def compare_runs(a: RunResult, b: RunResult, name: str = "determinism") -> GateCheck:
    failures = []
    pairs = [
        ("evaluations", EVAL_COLS, ["strategy_id", "session_id", "slot"]),
        ("transitions", TRANS_COLS, ["strategy_id", "session_id", "slot"]),
        ("candidates", CAND_COLS, ["signal_key"]),
        ("reference_positions", REF_COLS, ["strategy_id", "session_id", "entry_slot"]),
    ]
    checked = 0
    for table, cols, key in pairs:
        x, y = _canon(getattr(a, table), cols, key), _canon(getattr(b, table), cols, key)
        checked += len(x)
        if x != y:
            diff = sorted(set(x) ^ set(y))[:5]
            failures.append({"table": table, "rows_a": len(x), "rows_b": len(y), "examples": diff})
    return GateCheck(name, not failures, checked, failures)


def _upto(result: RunResult, m: int) -> RunResult:
    """Records a run had produced by the end of slot m."""

    def cut(df, col):
        return df[df[col] <= m] if not df.empty else df

    ref = result.reference_positions
    ref = ref[ref["exit_slot"] <= m] if not ref.empty else ref
    return RunResult(
        result.manifest,
        cut(result.evaluations, "slot"),
        cut(result.transitions, "slot"),
        cut(result.candidates, "trigger_slot"),
        ref,
        cut(result.snapshots, "slot"),
    )


def truncated_session(
    out: SessionOutput,
    store: HistoryStore,
    m: int,
    elig: EligibilityConfig,
    fcfg: FeatureConfig,
    usage: Usage,
) -> SessionOutput:
    """The session as it would look at the end of slot m: trades cut off at bar_end(m)."""
    d = out.day.session_date
    trades = store.read("spy_trades", d).to_pandas()
    cutoff = pd.Timestamp(out.day.open_utc + timedelta(minutes=m))
    bars = (
        build_session_bars(trades[trades["ts"] < cutoff], out.day, elig, usage).to_pandas().iloc[:m]
    )
    features, levels = compute_session_features(
        bars,
        out.session_id,
        out.prior,
        out.premarket_high,
        out.premarket_low,
        out.rvol_baseline,
        fcfg,
    )
    return SessionOutput(
        out.day,
        out.session_id,
        bars,
        features,
        levels,
        out.premarket_flags,
        out.prior,
        out.rvol_baseline,
        out.premarket_high,
        out.premarket_low,
    )


def check_truncation(
    sessions: Iterable[SessionOutput],
    cut_points: Callable[[SessionOutput], list[int]],
    workers: Callable[[], list[StrategyWorker]],
    store: HistoryStore,
    usage: Usage,
    dataset_id: str,
    elig: EligibilityConfig | None = None,
    fcfg: FeatureConfig | None = None,
) -> GateCheck:
    elig, fcfg = elig or EligibilityConfig(), fcfg or FeatureConfig.load()
    failures, checked = [], 0
    for out in sessions:
        full = ReplayRun(
            workers(), usage, features_cfg=fcfg, run_id="full", dataset_id=dataset_id
        ).run_outputs([out])
        for m in cut_points(out):
            part_out = truncated_session(out, store, m, elig, fcfg, usage)
            part = ReplayRun(
                workers(), usage, features_cfg=fcfg, run_id="part", dataset_id=dataset_id
            ).run_outputs([part_out])
            res = compare_runs(_upto(full, m), part, name=f"{out.session_id}@{m}")
            checked += 1
            if not res.passed:
                failures.append({"session": out.session_id, "m": m, "detail": res.failures[:2]})
    return GateCheck("no_look_ahead_truncation", not failures, checked, failures)


def check_traceability(result: RunResult) -> GateCheck:
    failures = []
    snaps = (
        result.snapshots.set_index("snapshot_id") if not result.snapshots.empty else pd.DataFrame()
    )
    known = set(snaps.index) if not snaps.empty else set()
    # 1. every snapshot hash re-verifies from its stored canonical identity
    for sid, ident in snaps["identity"].items() if not snaps.empty else []:
        if hashlib.sha256(ident.encode()).hexdigest() != sid:
            failures.append({"kind": "snapshot_hash_mismatch", "snapshot_id": sid})
    # 2. every evaluation and snapshot-caused transition points at a stored snapshot
    for sid in result.evaluations["snapshot_id"]:
        if sid not in known:
            failures.append({"kind": "evaluation_without_snapshot", "snapshot_id": sid})
    tr = result.transitions
    if not tr.empty:
        for r in tr[tr["cause_type"] == "snapshot"].itertuples():
            if r.snapshot_id not in known:
                failures.append(
                    {"kind": "transition_without_snapshot", "transition_id": r.transition_id}
                )
        ref_ids = set(result.reference_positions.get("reference_position_id", []))
        for r in tr[tr["cause_type"] == "reference_exit"].itertuples():
            if r.cause_record_id not in ref_ids:
                failures.append(
                    {"kind": "exit_without_reference_position", "transition_id": r.transition_id}
                )
    # 3. every candidate: stored snapshot at its trigger slot, entry = that snapshot's close
    for c in result.candidates.itertuples() if not result.candidates.empty else []:
        if c.snapshot_id not in known:
            failures.append({"kind": "candidate_without_snapshot", "candidate_id": c.candidate_id})
            continue
        snap = snaps.loc[c.snapshot_id]
        values = json.loads(snap["identity"])["values"]
        if snap["slot"] != c.trigger_slot or abs(values["close"] - c.entry_reference) > 1e-6:
            failures.append({"kind": "candidate_snapshot_mismatch", "candidate_id": c.candidate_id})
    # 4. every reference position links to a candidate
    cands = set(result.candidates.get("candidate_id", []))
    for r in (
        result.reference_positions.itertuples() if not result.reference_positions.empty else []
    ):
        if r.candidate_id not in cands:
            failures.append(
                {"kind": "reference_position_without_candidate", "id": r.reference_position_id}
            )
    checked = len(result.evaluations) + len(tr) + len(result.candidates) + len(result.snapshots)
    return GateCheck("traceability", not failures, checked, failures)
