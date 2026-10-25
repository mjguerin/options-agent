# ruff: noqa
"""All five workers on real data, Feb 1 - Apr 30 2024 only (the window already inspected for
behaviour, access log #8). Integrity: contract compliance, determinism across two runs,
reference positions closed within their session, timing, record counts. Reports candidate
COUNTS only: no P&L, exit reasons or other performance measures."""

import json, time
from datetime import date
from pathlib import Path

from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.brt import BRT
from tournament.strategies.fbr import FBR
from tournament.strategies.orb import ORB
from tournament.strategies.vmr import VMR
from tournament.strategies.vpc import VPC

ROOT = Path(__file__).resolve().parents[2]
store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
END = date(2024, 4, 30)


def workers():
    return [VPC(), ORB(), BRT(), FBR(), VMR()]


t0 = time.time()
a = ReplayRun(workers(), Usage.DEVELOPMENT, dataset_id=ds).run_store(store, end=END)
elapsed = time.time() - t0
b = ReplayRun(workers(), Usage.DEVELOPMENT, dataset_id=ds).run_store(store, end=END)
out = a.write(ROOT / "data" / "replay")

ref = a.reference_positions
cand = a.candidates
same_session_close = bool((ref["exit_slot"].notna()).all()) and len(ref) == len(cand)
cols = ["strategy_id", "session_id", "slot", "outcome", "setup_state", "snapshot_id"]
print(
    json.dumps(
        {
            "sessions": a.manifest["sessions"],
            "seconds_per_session": round(elapsed / a.manifest["sessions"]["count"], 2),
            "contract_violations": 0,  # any violation raises and stops the run
            "deterministic_evaluations": a.evaluations[cols]
            .astype(str)
            .equals(b.evaluations[cols].astype(str)),
            "deterministic_signal_keys": sorted(cand["signal_key"])
            == sorted(b.candidates["signal_key"])
            if len(cand)
            else True,
            "evaluations_per_strategy": a.evaluations["strategy_id"].value_counts().to_dict(),
            "candidate_counts": cand["strategy_id"].value_counts().to_dict() if len(cand) else {},
            "every_reference_position_closed_in_session": same_session_close,
            "max_exit_slot": int(ref["exit_slot"].max()) if len(ref) else None,
            "outcomes_per_strategy": {
                k: g["outcome"].value_counts().to_dict()
                for k, g in a.evaluations.groupby("strategy_id")
            },
            "written_to": str(out.relative_to(ROOT)),
        },
        indent=1,
        default=str,
    )
)
