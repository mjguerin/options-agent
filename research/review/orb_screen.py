# ruff: noqa
"""ORB Screen run (access log #29), exactly as registered in strategy_review_registry.md (ORB-H1,
frozen by the SHA-256 of this file and of config/strategies/orb-v2a.yaml, recorded there). Reads 2024-05-01 to 2024-08-31 only. Aggregates only."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.harness import ReplayRun, dataset_id_for
from tournament.strategies.orb import ORB

ROOT = Path(__file__).resolve().parents[2]
START, END = date(2024, 5, 1), date(2024, 8, 31)
SEED, B = 20261120, 10_000
MIN_DIFF, MIN_RATIO, MAX_RATIO = 0.05, 0.70, 1.30
VARIANTS = {"ORB-H1 let winners run": "orb-v2a"}
V1_INV = 0.25  # R is measured in v1's risk unit so a wider stop does not shrink every R

store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
SESSIONS = [o.session_id for o in outs]
MONTH = {o.session_id: str(o.day.session_date)[:7] for o in outs}
dates = [str(o.day.session_date) for o in outs]
assert min(dates) >= "2024-05-01" and max(dates) <= "2024-08-31", "outside the Screen partition"


def load(name):
    return yaml.safe_load((ROOT / "config/strategies" / f"{name}.yaml").read_text())


def positions(w) -> pd.DataFrame:
    run = ReplayRun([w], Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
    r = run.reference_positions
    r = r[r.reference_exit_price.notna()].copy()
    feat = run.candidates.set_index("candidate_id").features.map(json.loads)
    width = r.candidate_id.map(lambda c: feat[c]["orh"] - feat[c]["orl"]).astype(float)
    sign = np.where(r.underlying_direction == "bullish", 1.0, -1.0)
    pts = sign * (r.reference_exit_price.astype(float) - r.reference_entry_price.astype(float))
    unit = r.r_u.astype(float) - (w.p["inv_frac"] - V1_INV) * width  # v1's risk for this entry
    r["R"] = pts / unit
    r["R_own"] = pts / r.r_u.astype(float)
    r["key"] = list(zip(r.session_id, r.entry_slot.astype(int), r.underlying_direction))
    r["month"] = r.session_id.map(MONTH)
    return r[["session_id", "key", "month", "R", "R_own"]]


def per_session(df):
    g = df.groupby("session_id").R.agg(["sum", "count"]).reindex(SESSIONS).fillna(0.0)
    return g["sum"].to_numpy(), g["count"].to_numpy()


def describe(df):
    n = len(df)
    wins, losses = df.R[df.R > 0], df.R[df.R < 0]
    return {
        "n": n,
        "per_session": round(n / len(SESSIONS), 3),
        "mean_R": round(float(df.R.mean()), 3) if n else None,
        "mean_R_own_unit": round(float(df.R_own.mean()), 3) if n else None,
        "median_R": round(float(df.R.median()), 3) if n else None,
        "win_share": round(float((df.R > 0).mean()), 3) if n else None,
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2)
        if len(wins) and len(losses)
        else None,
        "total_R_per_session": round(float(df.R.sum() / len(SESSIONS)), 3),
    }


def compare(v1, var):
    s1, n1 = per_session(v1)
    s2, n2 = per_session(var)
    pooled = lambda s, c: s.sum() / c.sum() if c.sum() else np.nan
    diff = pooled(s2, n2) - pooled(s1, n1)
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(SESSIONS), size=(B, len(SESSIONS)))
    a = s2[idx].sum(1) / np.maximum(n2[idx].sum(1), 1) - s1[idx].sum(1) / np.maximum(
        n1[idx].sum(1), 1
    )
    months = {}
    for m in sorted(set(MONTH.values())):
        a1, a2 = v1[v1.month == m].R, var[var.month == m].R
        months[m[5:]] = round(float(a2.mean() - a1.mean()), 3) if len(a1) and len(a2) else None
    k1, k2 = set(v1.key), set(var.key)
    rem, kept, add = v1[v1.key.isin(k1 - k2)], v1[v1.key.isin(k1 & k2)], var[var.key.isin(k2 - k1)]
    ratio = len(var) / len(v1) if len(v1) else float("nan")
    lower = float(np.percentile(a, 20))
    return {
        "diff": round(float(diff), 3),
        "lower80": round(lower, 3),
        "freq_ratio": round(ratio, 3),
        "months": months,
        "removed": (len(rem), None if not len(rem) else round(float(rem.R.mean()), 3)),
        "kept": (len(kept), None if not len(kept) else round(float(kept.R.mean()), 3)),
        "added": (len(add), None if not len(add) else round(float(add.R.mean()), 3)),
        "PASS": bool(diff >= MIN_DIFF and lower > 0 and MIN_RATIO <= ratio <= MAX_RATIO),
        "why": [
            w
            for ok, w in [
                (diff >= MIN_DIFF, f"diff<{MIN_DIFF}"),
                (lower > 0, "lower80<=0"),
                (MIN_RATIO <= ratio <= MAX_RATIO, "frequency band"),
            ]
            if not ok
        ],
    }


if __name__ == "__main__":
    v1cfg = load("orb-v1")
    v1 = positions(ORB(config=v1cfg))
    default = positions(ORB())
    assert (
        len(v1) == len(default)
        and (v1.R.to_numpy() == default.R.to_numpy()).all()
        and list(v1.key) == list(default.key)
    ), "v1 must reproduce"
    # the same check for the variants' machinery: a copy of v1's config under a variant name
    twin = json.loads(json.dumps(v1cfg))
    twin["version"] = "orb-twin"
    t = positions(ORB(config=twin))
    assert len(t) == len(v1) and (t.R.to_numpy() == v1.R.to_numpy()).all(), (
        "a variant with v1's values must reproduce v1"
    )
    print(f"SCREEN {START} to {END}: {len(SESSIONS)} sessions; dataset {ds}; sanity check passed")
    out = {"v1": describe(v1)}
    print("v1:", out["v1"])
    rows = []
    for label, name in VARIANTS.items():
        var = positions(ORB(config=load(name)))
        d = describe(var)
        c = compare(v1, var)
        out[label] = {**d, **c}
        rows.append({"hypothesis": label, **d, **c})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 60)
    print("\nPER VARIANT")
    print(
        df[
            [
                "hypothesis",
                "n",
                "per_session",
                "mean_R",
                "mean_R_own_unit",
                "median_R",
                "win_share",
                "payoff",
                "total_R_per_session",
            ]
        ].to_string(index=False)
    )
    print("\nPAIRED AGAINST v1 (bars: diff >= +0.05R, 80% lower bound > 0, frequency 70% to 130%)")
    print(
        df[
            [
                "hypothesis",
                "diff",
                "lower80",
                "freq_ratio",
                "months",
                "removed",
                "kept",
                "added",
                "PASS",
                "why",
            ]
        ].to_string(index=False)
    )
    o = ROOT / "data" / "reports"
    o.mkdir(parents=True, exist_ok=True)
    (o / "orb_screen.json").write_text(json.dumps(out, indent=1, default=str))
