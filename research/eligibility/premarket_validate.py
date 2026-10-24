# ruff: noqa
"""Validate the raw pre-market SPY trades (spy_trades_premarket). Integrity only.

Checks per session: file present; every timestamp in [04:00 ET, open); sorted; provenance commit;
tape. Then: condition-code inventory against the matrix (unknown codes flagged), share of trades and
volume that E-6 would treat as price-eligible, and agreement of daily volume with the provider's
pre-market bar volume (SIP volume rule, informational codes excluded).
"""

import collections, csv, json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from tournament.marketdata.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
s = HistoryStore(ROOT / "data" / "history")
cal = pq.read_table(s.root / "calendar.parquet").to_pandas()
rows = list(csv.DictReader(open(ROOT / "config/trade_eligibility/cts_sale_conditions.csv")))
known = {r["condition_code"] for r in rows}
vol_yes = {r["condition_code"] for r in rows if r["volume_rule"] == "YES"}
last_yes = {r["condition_code"] for r in rows if r["consolidated_last_rule"] in ("YES",)}

problems, commits, tapes = [], collections.Counter(), collections.Counter()
codes, combos = collections.Counter(), collections.Counter()
trades_total = vol_total = 0
e6_trades = e6_vol = 0
vol_match = vol_days = 0
empty = []
for r in cal.itertuples():
    d = r.session_date if isinstance(r.session_date, date) else r.session_date.date()
    if d > date(2026, 10, 22):
        continue
    p = s.path("spy_trades_premarket", d)
    if not p.is_file():
        problems.append(f"{d} missing")
        continue
    commits[s.provenance(p).get("code_commit")] += 1
    t = pq.read_table(p).to_pandas()
    if t.empty:
        empty.append(str(d))
        continue
    start = pd.Timestamp(d).tz_localize("America/New_York") + pd.Timedelta(hours=4)
    if (t["ts"] < start.tz_convert("UTC")).any() or (t["ts"] >= pd.Timestamp(r.open_utc)).any():
        problems.append(f"{d} timestamp outside window")
    if not t["ts"].is_monotonic_increasing:
        problems.append(f"{d} not sorted")
    tapes.update(t["tape"].tolist())
    keys = t["conditions"].map(lambda c: "|".join("SPACE" if x == " " else x for x in c))
    combos.update(keys.tolist())
    for k in keys.unique():
        for c in k.split("|"):
            codes[c] += int((keys == k).sum())
    trades_total += len(t)
    vol_total += int(t["size"].sum())
    # E-6: T is the marker; every other condition must be consolidated-last YES
    e6 = keys.map(lambda k: all(c == "T" or c in last_yes for c in k.split("|")))
    e6_trades += int(e6.sum())
    e6_vol += int(t.loc[e6, "size"].sum())
    # provider pre-market bar volume vs our SIP-volume-rule sum
    vmask = keys.map(
        lambda k: (
            all(c in vol_yes for c in k.split("|"))
            and not any(c in ("M", "Q", "9") for c in k.split("|"))
        )
    )
    ours = int(t.loc[vmask, "size"].sum())
    bars = s.read("spy_bars_1m", d).to_pandas()
    prov = int(
        bars.loc[
            (bars["ts"] >= start.tz_convert("UTC"))
            & ~bars["regular_session"]
            & (bars["ts"] < pd.Timestamp(r.open_utc)),
            "volume",
        ].sum()
    )
    vol_days += 1
    vol_match += int(ours == prov)

unknown = sorted(c for c in codes if c not in known)
print(
    json.dumps(
        {
            "sessions_checked": len(
                cal[
                    cal.session_date.map(
                        lambda x: (x if isinstance(x, date) else x.date()) <= date(2026, 10, 22)
                    )
                ]
            ),
            "problems": problems[:20],
            "problem_count": len(problems),
            "empty_sessions": empty,
            "provenance_commits": dict(commits),
            "tapes": dict(tapes),
            "trades": trades_total,
            "volume": vol_total,
            "codes": dict(codes.most_common()),
            "unknown_codes": unknown,
            "top_combinations": dict(combos.most_common(12)),
            "distinct_combinations": len(combos),
            "e6_price_eligible_share": {
                "trades": round(e6_trades / trades_total, 4),
                "volume": round(e6_vol / vol_total, 4),
            },
            "daily_volume_equal_to_provider_premarket_bars": f"{vol_match}/{vol_days}",
        },
        indent=1,
    )
)
