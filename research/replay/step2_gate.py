# ruff: noqa
"""Step 2 gate run (integrity): determinism, traceability and whole-chain no-look-ahead
truncation, all five workers, Feb 1 - Apr 30 2024 (the window already inspected, access log #8).
Compares records for equality only; summarizes no strategy performance."""

import json, random, time
from datetime import date
from pathlib import Path

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.gate import check_traceability, check_truncation, compare_runs
from tournament.replay.harness import ReplayRun, code_commit, dataset_id_for
from tournament.strategies.brt import BRT
from tournament.strategies.fbr import FBR
from tournament.strategies.orb import ORB
from tournament.strategies.vmr import VMR
from tournament.strategies.vpc import VPC

ROOT = Path(__file__).resolve().parents[2]
START, END = date(2024, 2, 1), date(2024, 4, 30)
store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)


def workers():
    return [VPC(), ORB(), BRT(), FBR(), VMR()]


def cut_points(out):
    n = len(out.features)
    rng = random.Random(out.session_id)
    fixed = [15, 16, 31, 60, 61, n - 30, n - 29, n - 10, n]
    return sorted(set(fixed + rng.sample(range(62, n - 31), 3)))


t0 = time.time()
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
a = ReplayRun(workers(), Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
b = ReplayRun(workers(), Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
det = compare_runs(a, b)
trace = check_traceability(a)
trunc = check_truncation(outs, cut_points, workers, store, Usage.DEVELOPMENT, ds)
report = {
    "gate": "step2",
    "window": [str(START), str(END)],
    "sessions": len(outs),
    "dataset_id": ds,
    "code_commit": code_commit(),
    "elapsed_s": round(time.time() - t0, 1),
    "records": a.manifest["counts"],
    "checks": [det.as_dict(), trace.as_dict(), trunc.as_dict()],
    "passed": det.passed and trace.passed and trunc.passed,
}
out = ROOT / "data" / "reports"
out.mkdir(parents=True, exist_ok=True)
(out / f"step2_gate_{START}_{END}.json").write_text(json.dumps(report, indent=1, default=str))
print(json.dumps(report, indent=1, default=str))
