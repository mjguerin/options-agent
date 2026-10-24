# ruff: noqa
"""D-04 evidence, round 2 (integrity; bar construction and condition coverage only).

1. Effect of the state fix: trades whose open/close eligibility changes vs the v1-draft rule.
2. Per-period coverage of O and N: trades, sessions and regular slots, on the full store.
3. Round-lot structural break: share of trades, volume and notional in 40-99-share trades and
   in odd lots ('I'), before and after 2025-11-03.
4. The provider-open cases: every 09:30 minute where the provider open differs from ours, with
   the candidate prints (timestamp, conditions, exchange, size, price).
"""

import collections, json, sys
from datetime import date

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import bar_variants as bv
from bar_variants import load_matrix, classify, resolve_last, resolve_last_old, build, s, sample

out = {}
# 1. state fix effect on the 20 diagnostic sessions
changed = 0
total = 0
for d in sample:
    t = pq.read_table(s.path("spy_trades", d)).to_pandas()
    df = classify(t, d, load_matrix(d), "current")
    a, b = resolve_last(df), resolve_last_old(df)
    changed += int((a != b).sum())
    total += len(df)
out["state_fix_trades_changed"] = f"{changed} of {total}"

# 2. O / N coverage, all sessions
cov = {}
for code, cut in (("O", date(2024, 3, 28)), ("N", date(2024, 5, 28))):
    stats = collections.defaultdict(lambda: [0, 0, set()])
    for d in s.days("spy_trades"):
        t = pq.read_table(s.path("spy_trades", d), columns=["ts", "conditions"]).to_pandas()
        hit = t[t["conditions"].map(lambda c: code in c)]
        if len(hit):
            k = "before" if d < cut else "on_or_after"
            stats[k][0] += len(hit)
            stats[k][1] += 1
            stats[k][2] |= {(d, m) for m in hit["ts"].dt.floor("min")}
    cov[code] = {
        k: {"trades": v[0], "sessions": v[1], "slots": len(v[2])} for k, v in stats.items()
    }
    cov[code]["change_date"] = str(cut)
out["coverage"] = cov

# 3. round-lot break
rl = {}
for label, days in (
    (
        "2025-10-27..31",
        [d for d in s.days("spy_trades") if date(2025, 10, 27) <= d <= date(2025, 10, 31)],
    ),
    (
        "2025-11-03..07",
        [d for d in s.days("spy_trades") if date(2025, 11, 3) <= d <= date(2025, 11, 7)],
    ),
):
    acc = collections.Counter()
    for d in days:
        t = pq.read_table(
            s.path("spy_trades", d), columns=["price", "size", "conditions"]
        ).to_pandas()
        t["notional"] = t["price"] * t["size"]
        mid = t["size"].between(40, 99)
        odd = t["conditions"].map(lambda c: "I" in c)
        for name, m in (("size_40_99", mid), ("odd_lot_I", odd)):
            acc[(name, "trades")] += int(m.sum())
            acc[(name, "volume")] += int(t.loc[m, "size"].sum())
            acc[(name, "notional")] += float(t.loc[m, "notional"].sum())
        acc[("all", "trades")] += len(t)
        acc[("all", "volume")] += int(t["size"].sum())
        acc[("all", "notional")] += float(t["notional"].sum())
    rl[label] = {
        f"{n} share of {m}": round(acc[(n, m)] / acc[("all", m)], 4)
        for n in ("size_40_99", "odd_lot_I")
        for m in ("trades", "volume", "notional")
    }
out["round_lot_break"] = rl

# 4. provider-open cases
cases = []
for d in sample:
    t = pq.read_table(s.path("spy_trades", d)).to_pandas()
    alp = s.read("spy_bars_1m", d).to_pandas()
    alp = alp[alp["regular_session"]].set_index("ts")
    df = classify(t, d, load_matrix(d), "current")
    df["minute"] = df["ts"].dt.floor("min")
    ours = build(df, "last", "volume").reindex(alp.index)
    m0 = alp.index[0]
    if abs(float(alp.at[m0, "open"]) - float(ours.at[m0, "open"])) > 1e-6:
        first = df[df["minute"] == m0].head(6)
        el = resolve_last(df)[: len(first)]
        cases.append(
            {
                "session": str(d),
                "provider_open": float(alp.at[m0, "open"]),
                "our_open": float(ours.at[m0, "open"]),
                "first_prints": [
                    {
                        "t_offset_s": round((r.ts - m0).total_seconds(), 6),
                        "conds": "|".join(r.conditions),
                        "exch": r.exchange,
                        "size": int(r.size),
                        "price": float(r.price),
                        "our_oc": bool(e),
                    }
                    for r, e in zip(first.itertuples(), el)
                ],
            }
        )
out["provider_open_cases"] = cases
print(json.dumps(out, indent=1, default=str))
