# ruff: noqa
"""ORB-H1 option-level check and Validate run (access logs #30 and #31).

  python orb_validate.py design    DEBUG on Design: coverage counts and aggregate signs only.
  python orb_validate.py validate  The Validate run of ORB-H1 on 2024-09-01 to 2024-12-31, once, with the
                                   bars written in strategy_review_protocol.md section 5 and the method
                                   in option_level_replay_method.md (dated and frozen before this ran).
Aggregates only. Never reads Confirm or live data."""

import json, sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from option_replay import score

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.orb import ORB

ROOT = Path(__file__).resolve().parents[2]
MODE = sys.argv[1]
WINDOWS = {"design": (date(2024, 2, 1), date(2024, 4, 30)), "validate": (date(2024, 9, 1), date(2024, 12, 31))}
START, END = WINDOWS[MODE]
SEED, B = 20261120, 10_000
V1_INV, M_TESTS = 0.25, 1  # one registered hypothesis in Round 2: Holm over 1 test changes nothing
Z95 = 95

store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
SESSIONS = [o.session_id for o in outs]
DAY = {o.session_id: o.day.session_date for o in outs}
MONTH = {s: str(DAY[s])[:7] for s in SESSIONS}
assert min(DAY.values()) >= START and max(DAY.values()) <= END
if MODE == "validate":
    assert min(DAY.values()) >= date(2024, 9, 1) and max(DAY.values()) <= date(2024, 12, 31)


def load(name):
    return yaml.safe_load((ROOT / "config/strategies" / f"{name}.yaml").read_text())


def positions(w):
    run = ReplayRun([w], Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
    r = run.reference_positions
    r = r[r.reference_exit_price.notna()].copy()
    feat = run.candidates.set_index("candidate_id").features.map(json.loads)
    width = r.candidate_id.map(lambda c: feat[c]["orh"] - feat[c]["orl"]).astype(float)
    sign = np.where(r.underlying_direction == "bullish", 1.0, -1.0)
    pts = sign * (r.reference_exit_price.astype(float) - r.reference_entry_price.astype(float))
    r["R"] = pts / (r.r_u.astype(float) - (w.p["inv_frac"] - V1_INV) * width)
    r["key"] = list(zip(r.session_id, r.entry_slot.astype(int), r.underlying_direction))
    r["month"] = r.session_id.map(MONTH)
    return r


def pooled(df, col):
    g = df.groupby("session_id")[col].agg(["sum", "count"]).reindex(SESSIONS).fillna(0.0)
    return g["sum"].to_numpy(), g["count"].to_numpy()


def boot(v1, var, col, keep=None):
    s1, n1 = pooled(v1, col)
    s2, n2 = pooled(var, col)
    mask = np.ones(len(SESSIONS), bool) if keep is None else np.array([s in keep for s in SESSIONS])
    s1, n1, s2, n2 = s1[mask], n1[mask], s2[mask], n2[mask]
    diff = s2.sum() / max(n2.sum(), 1) - s1.sum() / max(n1.sum(), 1)
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(s1), size=(B, len(s1)))
    a = s2[idx].sum(1) / np.maximum(n2[idx].sum(1), 1) - s1[idx].sum(1) / np.maximum(n1[idx].sum(1), 1)
    return float(diff), a


if __name__ == "__main__":
    v1 = positions(ORB(config=load("orb-v1")))
    var = positions(ORB(config=load("orb-v2a")))
    sess = {s: DAY[s] for s in SESSIONS}
    o1, o2 = score(v1, sess), score(var, sess)
    cov = {}
    for name, o, p in (("v1", o1, v1), ("v2a", o2, var)):
        c = o.status.value_counts().to_dict()
        cov[name] = c
    bad = set(o1[o1.status == "UNPRICED"].session_id) | set(o2[o2.status == "UNPRICED"].session_id)
    keep = set(SESSIONS) - bad
    kept_pos = len(v1[v1.session_id.isin(keep)])
    print(f"{MODE.upper()} {START} to {END}: {len(SESSIONS)} sessions, dataset {ds}")
    print("positions: v1", len(v1), "v2a", len(var))
    print("option-level statuses:", cov)
    print(f"sessions dropped for an unpriced position: {len(bad)}; positions retained {kept_pos} of {len(v1)} ({kept_pos / max(len(v1), 1):.0%})")
    t1, t2 = o1[o1.status == "TRADED"], o2[o2.status == "TRADED"]
    d_opt, _ = boot(t1, t2, "pnl", keep)
    d_R = None
    if MODE == "design":
        print("AGGREGATE SIGNS ONLY (debug): R diff sign", "+" if (var.R.mean() - v1.R.mean()) > 0 else "-",
              "| option-level $ diff sign", "+" if d_opt > 0 else "-")
        sys.exit()
    # ---------------------------------------------------------------- Validate bars
    d_R, a = boot(v1, var, "R")
    lower95 = float(np.percentile(a, 5))  # one-sided 95% bound
    ratio = (len(var) / len(SESSIONS)) / (len(v1) / len(SESSIONS))
    months = {}
    for m in sorted(set(MONTH.values())):
        a1, a2 = v1[v1.month == m].R, var[var.month == m].R
        months[m[5:]] = round(float(a2.mean() - a1.mean()), 3) if len(a1) and len(a2) else None
    worst = min(v for v in months.values() if v is not None)
    cum = lambda df: float(df.sort_values("session_id").R.cumsum().pipe(lambda c: (c.cummax() - c).max()))
    print(f"\nv1: n={len(v1)} mean R {v1.R.mean():+.3f} win {(v1.R > 0).mean():.2f} | v2a: n={len(var)} mean R {var.R.mean():+.3f} win {(var.R > 0).mean():.2f} median {var.R.median():+.3f}")
    print(f"R paired difference {d_R:+.3f}; one-sided 95% lower bound {lower95:+.3f}; frequency ratio {ratio:.2f}; by month {months}")
    print(f"option level: v1 ${t1.pnl.mean():+.1f} per position (n={len(t1)}), v2a ${t2.pnl.mean():+.1f} (n={len(t2)}); paired difference ${d_opt:+.1f} per position over retained sessions")
    ok = {
        "bound95>0 (Holm over 1 test)": lower95 > 0,
        "frequency within 30%": 0.7 <= ratio <= 1.3,
        "worst month >= -0.30R": worst >= -0.30,
        "option-level diff positive, same sign as R": d_opt > 0 and d_R > 0,
        "option-level evaluable (>=80% positions)": kept_pos / max(len(v1), 1) >= 0.8,
    }
    print("\nVALIDATE BARS:", ok)
    print("PASS" if all(ok.values()) else "FAIL")
