# ruff: noqa
"""Live against replay (access log #24): do the live shadow sessions behave like the replay of the
development window? AGGREGATES ONLY: per strategy, the number of reference positions, share with a
positive result, mean and median result in R, exit-reason shares, mean best and worst excursion in
R and signals per session. Prints no dates, trades, curves or option prices. It changes nothing."""

import glob
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.brt import BRT
from tournament.strategies.fbr import FBR
from tournament.strategies.orb import ORB
from tournament.strategies.vmr import VMR
from tournament.strategies.vpc import VPC

ROOT = Path(__file__).resolve().parents[2]
START, END = date(2024, 2, 1), date(2024, 4, 30)


def outcomes(refs: pd.DataFrame, bars_by_session: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for _, r in refs.iterrows():
        if pd.isna(r["reference_exit_price"]):
            continue
        sign = 1 if r["underlying_direction"] == "bullish" else -1
        entry, ru = float(r["reference_entry_price"]), float(r["r_u"])
        held = bars_by_session[r["session_id"]]
        held = held[(held["slot"] > int(r["entry_slot"])) & (held["slot"] <= int(r["exit_slot"]))]
        best = (held["high"].max() if sign > 0 else held["low"].min()) if len(held) else entry
        worst = (held["low"].min() if sign > 0 else held["high"].max()) if len(held) else entry
        rows.append(
            {
                "strategy": r["strategy_id"],
                "result_r": sign * (float(r["reference_exit_price"]) - entry) / ru,
                "best_r": sign * (best - entry) / ru,
                "worst_r": sign * (worst - entry) / ru,
                "reason": r["exit_primary_reason"],
                "slots": int(r["exit_slot"]) - int(r["entry_slot"]),
            }
        )
    return pd.DataFrame(rows)


def summarize(o: pd.DataFrame, sessions: int, candidates: pd.DataFrame) -> pd.DataFrame:
    out = []
    for sid in ["vpc", "orb", "brt", "fbr", "vmr"]:
        g = o[o.strategy == sid]
        n = len(g)
        reasons = g.reason.value_counts(normalize=True).round(2).to_dict() if n else {}
        out.append(
            {
                "strategy": sid,
                "signals/session": round(int((candidates.strategy_id == sid).sum()) / sessions, 2),
                "positions": n,
                "win share": round(float((g.result_r > 0).mean()), 2) if n else None,
                "mean R": round(float(g.result_r.mean()), 2) if n else None,
                "median R": round(float(g.result_r.median()), 2) if n else None,
                "mean best R": round(float(g.best_r.mean()), 2) if n else None,
                "mean worst R": round(float(g.worst_r.mean()), 2) if n else None,
                "mean slots held": round(float(g.slots.mean()), 1) if n else None,
                "exit reasons": reasons,
            }
        )
    return pd.DataFrame(out)


# ---- replay ----
store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
run = ReplayRun([VPC(), ORB(), BRT(), FBR(), VMR()], Usage.DEVELOPMENT, dataset_id=ds).run_outputs(
    outs
)
bars = {o.session_id: o.bars for o in outs}
replay = outcomes(run.reference_positions, bars)
# ---- live ----
refs, cands, lbars, nlive = [], [], {}, 0
for d in sorted(glob.glob(str(ROOT / "data/live/2026-10-2[789]/shadow"))):
    rp = glob.glob(d + "/records/*/reference_positions.parquet")
    if not rp:
        continue
    nlive += 1
    r = pd.read_parquet(rp[0])
    refs.append(r)
    cands.append(pd.read_parquet(glob.glob(d + "/records/*/candidates.parquet")[0]))
    b = pd.read_parquet(d + "/bars.parquet")
    for sid in r.session_id.unique():
        lbars[sid] = b
live_refs = pd.concat(refs, ignore_index=True)
live = outcomes(live_refs, lbars)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_colwidth", 70)
print(f"REPLAY, development window ({len(outs)} sessions)")
print(summarize(replay, len(outs), run.candidates).to_string(index=False))
print(f"\nLIVE shadow ({nlive} sessions)")
print(summarize(live, nlive, pd.concat(cands, ignore_index=True)).to_string(index=False))
print(
    "\nall strategies pooled: replay win share %.2f mean R %.2f (n=%d) | live win share %.2f mean R %.2f (n=%d)"
    % (
        (replay.result_r > 0).mean(),
        replay.result_r.mean(),
        len(replay),
        (live.result_r > 0).mean(),
        live.result_r.mean(),
        len(live),
    )
)
