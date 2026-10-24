# ruff: noqa
"""Share of trades flagged odd lot ('I') by size band, around the Nov 3 2025 round-lot change.

Integrity check on condition flags and trade sizes only; no prices are read.
"""

from datetime import date

import pyarrow.parquet as pq

from tournament.marketdata.store import HistoryStore

s = HistoryStore("data/history")
days = s.days("spy_trades")
pick = sorted(
    {
        *(d for d in days if date(2025, 10, 28) <= d <= date(2025, 11, 6)),
        days[0],
        days[300],
        days[-1],
    }
)
bands = [(1, 39), (40, 99), (100, None)]
print("session      " + "   ".join(f"size {lo}-{hi or '+'}: I share" for lo, hi in bands))
for d in pick:
    df = pq.read_table(s.path("spy_trades", d), columns=["size", "conditions"]).to_pandas()
    df["odd"] = df["conditions"].map(lambda c: "I" in c)
    cells = []
    for lo, hi in bands:
        m = (df["size"] >= lo) & ((df["size"] <= hi) if hi else True)
        cells.append(f"{df.loc[m, 'odd'].mean():.3f} (n={int(m.sum()):>7})")
    print(f"{d!s:12} " + "   ".join(cells))
