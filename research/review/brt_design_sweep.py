# ruff: noqa
"""BRT value selection on the DESIGN partition (access log #26; protocol 3a).

One parameter at a time from brt-v1, a declared grid, aggregates only. The selection rule is the
one written in the protocol before this ran. Reads 2024-02-01 to 2024-04-30 only."""

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
from tournament.strategies.brt import BRT

ROOT = Path(__file__).resolve().parents[2]
START, END = date(2024, 2, 1), date(2024, 4, 30)
GRIDS = {
    "brk_min": [0.25, 0.35, 0.5, 0.75, 1.0],
    "retest_wick_max": [1.0, 0.85, 0.7, 0.6, 0.5],
    "rr_min": [1.2, 1.35, 1.5, 1.75, 2.0],
}  # the first value of each grid is v1's
SEED, B = 20261120, 10_000
MIN_FREQ_RATIO = 0.70

V1 = yaml.safe_load((ROOT / "config/strategies/brt-v1.yaml").read_text())
store = HistoryStore(ROOT / "data" / "history")
ds = dataset_id_for(store)
outs = list(run_sessions(store, Usage.DEVELOPMENT, START, END))
SESSIONS = [o.session_id for o in outs]
MONTH = {o.session_id: str(o.day.session_date)[:7] for o in outs}
assert (
    min(str(o.day.session_date) for o in outs) >= "2024-02-01"
    and max(str(o.day.session_date) for o in outs) <= "2024-04-30"
)


def worker(**changes):
    cfg = json.loads(json.dumps(V1))
    cfg["params"].update(changes)
    return BRT(config=cfg)


def positions(w) -> pd.DataFrame:
    run = ReplayRun([w], Usage.DEVELOPMENT, dataset_id=ds).run_outputs(outs)
    r = run.reference_positions
    r = r[r.reference_exit_price.notna()].copy()
    sign = np.where(r.underlying_direction == "bullish", 1.0, -1.0)
    r["R"] = (
        sign
        * (r.reference_exit_price.astype(float) - r.reference_entry_price.astype(float))
        / r.r_u.astype(float)
    )
    r["key"] = list(zip(r.session_id, r.entry_slot.astype(int), r.underlying_direction))
    r["month"] = r.session_id.map(MONTH)
    return r[["session_id", "key", "month", "R"]]


def per_session(df):
    g = df.groupby("session_id").R.agg(["sum", "count"]).reindex(SESSIONS).fillna(0.0)
    return g["sum"].to_numpy(), g["count"].to_numpy()


def stats(v1: pd.DataFrame, var: pd.DataFrame, is_base: bool) -> dict:
    s1, n1 = per_session(v1)
    s2, n2 = per_session(var)
    n = len(var)
    out = {
        "n": n,
        "per_session": round(n / len(SESSIONS), 3),
        "freq_ratio": round(n / len(v1), 3) if len(v1) else None,
        "mean_R": round(float(var.R.mean()), 3) if n else None,
        "median_R": round(float(var.R.median()), 3) if n else None,
        "win_share": round(float((var.R > 0).mean()), 3) if n else None,
    }
    if is_base:
        return out
    pooled = lambda s, c: s.sum() / c.sum() if c.sum() else np.nan
    diff = pooled(s2, n2) - pooled(s1, n1)
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(SESSIONS), size=(B, len(SESSIONS)))
    a = s2[idx].sum(1) / np.maximum(n2[idx].sum(1), 1) - s1[idx].sum(1) / np.maximum(
        n1[idx].sum(1), 1
    )
    out["diff"] = round(float(diff), 3)
    out["lower80"] = round(float(np.percentile(a, 20)), 3)
    months = {}
    for m in sorted(set(MONTH.values())):
        a1, a2 = v1[v1.month == m].R, var[var.month == m].R
        months[m[5:]] = round(float(a2.mean() - a1.mean()), 3) if len(a1) and len(a2) else None
    out["months"] = months
    k1, k2 = set(v1.key), set(var.key)
    rem, kept, add = v1[v1.key.isin(k1 - k2)], v1[v1.key.isin(k1 & k2)], var[var.key.isin(k2 - k1)]
    out["removed"] = (len(rem), round(float(rem.R.mean()), 3) if len(rem) else None)
    out["kept"] = (len(kept), round(float(kept.R.mean()), 3) if len(kept) else None)
    out["added"] = (len(add), round(float(add.R.mean()), 3) if len(add) else None)
    return out


def eligible(s: dict) -> bool:
    pos_months = sum(1 for v in s["months"].values() if v is not None and v > 0)
    return (
        s["freq_ratio"] is not None
        and s["freq_ratio"] >= MIN_FREQ_RATIO
        and s["diff"] > 0
        and pos_months >= 2
    )


def select(rows: list[dict]) -> dict | None:
    """rows[0] is v1; rows[1:] ordered away from it. Isolated eligible values (no eligible grid
    neighbour) are noise; take the remaining eligible value closest to v1."""
    el = [i for i in range(1, len(rows)) if eligible(rows[i])]
    keep = [i for i in el if (i - 1 in el) or (i + 1 in el)]
    return rows[min(keep)] if keep else None


if __name__ == "__main__":
    base_default = positions(BRT())
    v1 = positions(worker())
    assert (
        len(v1) == len(base_default)
        and (v1.R.to_numpy() == base_default.R.to_numpy()).all()
        and list(v1.key) == list(base_default.key)
    ), "variant with v1 values must reproduce v1"
    print(
        f"DESIGN {START} to {END}: {len(SESSIONS)} sessions; sanity check passed (a variant with v1's values reproduces v1)"
    )
    base = stats(v1, v1, True)
    print("v1:", base)
    result = {"v1": base}
    for param, grid in GRIDS.items():
        rows = [dict(value=grid[0], **base)]
        for val in grid[1:]:
            s = stats(v1, positions(worker(**{param: val})), False)
            rows.append(dict(value=val, **s))
        result[param] = rows
        print(f"\n== {param} (v1 = {grid[0]})")
        print(pd.DataFrame(rows).drop(columns=["median_R"]).to_string(index=False))
        sel = select(rows)
        flags = [("eligible" if eligible(r) else "no") for r in rows[1:]]
        print("eligibility by grid value:", dict(zip(grid[1:], flags)))
        print(
            "SELECTED:",
            None if sel is None else sel["value"],
            "" if sel else "-> no support in Design; v1's value stays",
        )
        result[param + "_selected"] = None if sel is None else sel["value"]
    out = ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "brt_design_sweep.json").write_text(json.dumps(result, indent=1, default=str))
