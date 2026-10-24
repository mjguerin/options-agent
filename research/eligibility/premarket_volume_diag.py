# ruff: noqa
"""Which trade population reproduces the provider's pre-market bar volume? (integrity)"""

import json
from datetime import date
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq
from tournament.marketdata.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
s = HistoryStore(ROOT / "data" / "history")
cal = pq.read_table(s.root / "calendar.parquet").to_pandas()
days = [d for d in s.days("spy_trades_premarket")][::34][:20]
variants = {
    "all": lambda k: True,
    "sip_volume_rule": lambda k: True,  # every code seen pre-market is volume YES
    "excl_C": lambda k: "C" not in k,
    "excl_U": lambda k: "U" not in k,
    "excl_C_U_B_R": lambda k: not ({"C", "U", "B", "R"} & k),
    "excl_odd_lots": lambda k: "I" not in k,
    "last_eligible_except_T": lambda k: k <= {"SPACE", "T", "F", "E", "K", "O", "X", "5", "6"},
}
res = {v: [] for v in variants}
minute_level = {v: [0, 0] for v in variants}
for d in days:
    r = cal[cal.session_date.map(lambda x: (x if isinstance(x, date) else x.date()) == d)].iloc[0]
    t = pq.read_table(s.path("spy_trades_premarket", d)).to_pandas()
    t["k"] = t["conditions"].map(lambda c: frozenset("SPACE" if x == " " else x for x in c))
    t["minute"] = t["ts"].dt.floor("min")
    start = (pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=4)).tz_convert(
        "UTC"
    )
    b = s.read("spy_bars_1m", d).to_pandas()
    b = b[(b["ts"] >= start) & (b["ts"] < pd.Timestamp(r.open_utc))].set_index("ts")
    for v, f in variants.items():
        sub = t[t["k"].map(f)]
        ours = sub.groupby("minute")["size"].sum().reindex(b.index).fillna(0)
        res[v].append(round(sub["size"].sum() / b["volume"].sum(), 5))
        minute_level[v][0] += int((ours == b["volume"]).sum())
        minute_level[v][1] += len(b)
print(
    json.dumps(
        {
            "sessions": [str(d) for d in days],
            "ratio_ours_over_provider_daily (median, min, max)": {
                v: (sorted(x)[len(x) // 2], min(x), max(x)) for v, x in res.items()
            },
            "minute_volume_equal": {v: f"{a}/{n}" for v, (a, n) in minute_level.items()},
        },
        indent=1,
    )
)

# Is the daily gap exactly the volume in minutes where the provider has no bar?
explained = []
nobar_minutes = 0
nobar_only_odd = 0
for d in days:
    r = cal[cal.session_date.map(lambda x: (x if isinstance(x, date) else x.date()) == d)].iloc[0]
    t = pq.read_table(s.path("spy_trades_premarket", d)).to_pandas()
    t["minute"] = t["ts"].dt.floor("min")
    start = (pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=4)).tz_convert(
        "UTC"
    )
    b = s.read("spy_bars_1m", d).to_pandas()
    b = b[(b["ts"] >= start) & (b["ts"] < pd.Timestamp(r.open_utc))].set_index("ts")
    per_min = t.groupby("minute")["size"].sum()
    missing = per_min[~per_min.index.isin(b.index)]
    gap = int(t["size"].sum() - b["volume"].sum())
    explained.append((str(d), gap, int(missing.sum())))
    nobar_minutes += len(missing)
    for m in missing.index:
        nobar_only_odd += int(t.loc[t["minute"] == m, "conditions"].map(lambda c: "I" in c).all())
print(
    json.dumps(
        {
            "daily_gap_vs_volume_in_no_bar_minutes": explained,
            "no_bar_minutes": nobar_minutes,
            "of_which_all_trades_odd_lot": nobar_only_odd,
        },
        indent=1,
    )
)
