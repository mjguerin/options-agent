# ruff: noqa
"""Feature pipeline smoke run on the development period (integrity: readiness, determinism,
warm-up equivalence, timing). Doesn't summarize market behaviour or strategy outcomes."""

import collections, json, time
from datetime import date
from pathlib import Path

import pandas as pd

from tournament.features.pipeline import run_sessions
from tournament.marketdata.eligibility import Usage
from tournament.marketdata.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
store = HistoryStore(ROOT / "data" / "history")
END = date(2024, 3, 28)

t0 = time.time()
run1 = {o.session_id: o for o in run_sessions(store, Usage.DEVELOPMENT, end=END)}
elapsed = time.time() - t0
run2 = {o.session_id: o for o in run_sessions(store, Usage.DEVELOPMENT, end=END)}
late = {
    o.session_id: o
    for o in run_sessions(store, Usage.DEVELOPMENT, start=date(2024, 3, 15), end=END)
}

COLS = [
    "a_cur",
    "atr_source",
    "disp_30_atr",
    "occ_up_30",
    "crosses_60",
    "regime",
    "rvol",
    "zone_version",
    "side",
]


def same(a, b):
    return all(a[c].astype(str).equals(b[c].astype(str)) for c in COLS) and list(
        a["swings_new"]
    ) == list(b["swings_new"])


first_ready = collections.defaultdict(list)
sessions_rvol_ready = 0
for sid, o in run1.items():
    f = o.features
    for c in ("a_cur", "disp_30_atr", "crosses_60", "rvol"):
        ok = f.index[f[c].notna()]
        first_ready[c].append(int(f.loc[ok[0], "slot"]) if len(ok) else None)
    sessions_rvol_ready += int(f["rvol"].notna().any())

report = {
    "sessions": len(run1),
    "elapsed_s_per_session": round(elapsed / len(run1), 2),
    "deterministic_rerun": all(same(run1[s].features, run2[s].features) for s in run1),
    "warmup_equivalent_from_2024_03_15": all(
        same(run1[s].features, late[s].features) for s in late
    ),
    "late_run_sessions": len(late),
    "first_ready_slot (min, max over sessions)": {
        c: (
            min(x for x in v if x is not None) if any(x is not None for x in v) else None,
            max(x for x in v if x is not None) if any(x is not None for x in v) else None,
            sum(x is None for x in v),
        )
        for c, v in first_ready.items()
    },
    "first_session_with_rvol": next(
        (s for s, o in run1.items() if o.features["rvol"].notna().any()), None
    ),
    "sessions_with_zv1": sum("zv1" in o.levels.zones for o in run1.values()),
    "sessions_with_zv2": sum("zv2" in o.levels.zones for o in run1.values()),
    "or_complete": sum(o.levels.or_complete for o in run1.values()),
    "premarket_flags": collections.Counter(f for o in run1.values() for f in o.premarket_flags),
}
print(json.dumps(report, indent=1, default=str))
