# ruff: noqa
"""Replay harness smoke run (integrity): a null worker that never emits, over development
sessions. Checks speed, record counts, writing, and snapshot-id stability across runs."""

import json, time
from datetime import date
from pathlib import Path

from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.base import Decision, Outcome, StrategyWorker

ROOT = Path(__file__).resolve().parents[2]


class Null(StrategyWorker):
    strategy_id, version, required_features = "null", "smoke-1", ("a_cur",)

    def reset_session(self, session_id):
        pass

    def evaluate(self, ctx):
        return Decision(Outcome.WATCHING if ctx.may_emit else Outcome.INELIGIBLE, "IDLE")


store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
t0 = time.time()
a = ReplayRun([Null({})], Usage.DEVELOPMENT, dataset_id=ds).run_store(store, end=date(2024, 3, 28))
elapsed = time.time() - t0
b = ReplayRun([Null({})], Usage.DEVELOPMENT, dataset_id=ds).run_store(store, end=date(2024, 3, 28))
out = a.write(ROOT / "data" / "replay")
print(
    json.dumps(
        {
            "dataset_id": ds,
            "sessions": a.manifest["sessions"],
            "counts": a.manifest["counts"],
            "seconds_per_session": round(elapsed / a.manifest["sessions"]["count"], 2),
            "snapshot_ids_stable_across_runs": a.evaluations["snapshot_id"].tolist()
            == b.evaluations["snapshot_id"].tolist(),
            "run_ids_differ": a.manifest["run_id"] != b.manifest["run_id"],
            "outcomes": a.evaluations["outcome"].value_counts().to_dict(),
            "not_ready_evaluations": int((a.evaluations["readiness_status"] == "NOT_READY").sum()),
            "written_to": str(out.relative_to(ROOT)),
            "files": sorted(p.name for p in out.iterdir()),
            "manifest_keys": sorted(a.manifest),
        },
        indent=1,
        default=str,
    )
)
