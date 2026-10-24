# ruff: noqa
"""D-04 (E) final diagnostic with adapter-v1-draft: HL = last set, VWAP = last set,
N (to 2024-05-27) volume YES, O backfilled. Classifies every remaining mismatch."""

import collections, json, sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import bar_variants as bv
from bar_variants import *  # noqa

orig = bv.classify


def classify_adapter(df, day, matrix, policy):
    out = orig(df, day, matrix, policy)
    if day <= date(2024, 5, 27):
        has_n = df["conditions"].map(lambda c: "N" in c)
        others_ok = df["conditions"].map(
            lambda c: all(
                x in ("N", " ")
                or matrix.get("SPACE" if x == " " else x, {}).get("volume_rule") == "YES"
                for x in c
            )
        )
        out.loc[has_n & others_ok, "vol"] = "YES"
    return out


fields = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]
tot = {f: [0, 0] for f in fields}
cats = collections.Counter()
for d in sample:
    t = pq.read_table(s.path("spy_trades", d)).to_pandas()
    alp = s.read("spy_bars_1m", d).to_pandas()
    alp = alp[alp["regular_session"]].set_index("ts")
    df = classify_adapter(t, d, load_matrix(d), "current")
    df["minute"] = df["ts"].dt.floor("min")
    ours = build(df, "last", "volume")
    el = resolve_last(df)
    g = df[el].assign(n=lambda x: x.price * x["size"]).groupby("minute")
    ours["vwap"] = g["n"].sum() / g["size"].sum()
    ours = ours.reindex(alp.index)
    first = alp.index[0]
    for f in fields:
        ok = np.isclose(
            alp[f].astype(float), ours[f].astype(float), atol=1e-6 if f != "vwap" else 5e-6
        )
        tot[f][0] += int(ok.sum())
        tot[f][1] += len(ok)
        for m in alp.index[~ok]:
            if f in ("open", "high", "low") and m == first:
                cats[(f, "09:30 Form T print in Alpaca's bar (deliberate SIP divergence)")] += 1
            elif f in ("volume", "trade_count") and ours.at[m, f] > alp.at[m, f]:
                cats[
                    (
                        f,
                        "ours higher: trade on the final tape, absent from Alpaca's bar (late-trade cutoff)",
                    )
                ] += 1
            else:
                cats[(f, "UNEXPLAINED")] += 1
print(
    json.dumps(
        {
            "sessions": len(sample),
            "match": {f: f"{a}/{b} = {a / b:.4%}" for f, (a, b) in tot.items()},
            "mismatch_categories": {f"{k[0]}: {k[1]}": n for k, n in sorted(cats.items())},
        },
        indent=1,
    )
)
