# ruff: noqa
"""Inventory sale-condition codes in stored SPY trades: counts by tape and condition set.

Integrity only: reads condition codes, tapes and counts. No prices or returns are summarized.
"""

import collections
import json
from datetime import date

import pyarrow.compute as pc
import pyarrow.parquet as pq

from tournament.marketdata.store import HistoryStore

s = HistoryStore("data/history")
days = s.days("spy_trades")
# Every 10th session across the whole store, plus first and last: coverage without reading 4 GB.
sample = sorted(set(days[::10]) | {days[0], days[-1]})
by_code = collections.Counter()
by_set = collections.Counter()
tapes = collections.Counter()
first_seen, last_seen = {}, {}
rows = 0
for d in sample:
    t = pq.read_table(s.path("spy_trades", d), columns=["conditions", "tape"])
    rows += t.num_rows
    for tape, n in zip(*pc.value_counts(t.column("tape")).flatten()):
        tapes[tape.as_py()] += n.as_py()
    for conds, n in zip(*pc.value_counts(
        pc.binary_join(pc.list_sort(t.column("conditions")) if hasattr(pc, "list_sort") else t.column("conditions"), "|")
    ).flatten()):
        key = conds.as_py()
        by_set[key] += n.as_py()
        for c in key.split("|"):
            by_code[c] += n.as_py()
            first_seen.setdefault(c, str(d))
            last_seen[c] = str(d)
print(json.dumps({
    "sessions_sampled": len(sample), "first": str(sample[0]), "last": str(sample[-1]),
    "trades": rows, "tapes": dict(tapes),
    "codes": {c: {"trades": n, "share": round(n / rows, 6), "first": first_seen[c], "last": last_seen[c]}
              for c, n in by_code.most_common()},
    "combinations": {k: n for k, n in by_set.most_common(40)},
    "distinct_combinations": len(by_set),
}, indent=1))
