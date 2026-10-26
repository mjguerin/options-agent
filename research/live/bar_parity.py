# ruff: noqa
"""Live/batch bar parity (integrity): stream each sampled session's stored trades through the
live builder in stored order, all on time, and require bars identical to the batch builder."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tournament.live.bars import LiveBarBuilder
from tournament.live.events import TradeIn
from tournament.marketdata.bars import BAR_SCHEMA, build_session_bars
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
store = HistoryStore(ROOT / "data" / "history")
cfg = EligibilityConfig()
cal = pd.read_parquet(ROOT / "data/history/calendar.parquet")
days = {
    r.session_date: SessionDay(
        r.session_date, r.open_utc.to_pydatetime(), r.close_utc.to_pydatetime()
    )
    for r in cal.itertuples()
}
picks = [date.fromisoformat(a) for a in sys.argv[1:]]
ok = True


def eq(a, b):
    if hasattr(a, "__len__") and not isinstance(a, str):
        return list(a) == list(b)
    if isinstance(a, float) and isinstance(b, float) and np.isnan(a) and np.isnan(b):
        return True
    return a == b


for d in picks:
    day = days[d]
    usage = Usage.DEVELOPMENT if d.year == 2024 else Usage.HOLDOUT
    trades = store.read("spy_trades", d).to_pandas()
    batch = build_session_bars(trades, day, cfg, usage).to_pandas()
    b = LiveBarBuilder(day, cfg, usage)
    t = trades.sort_values("ts", kind="stable")
    rows, nxt = [], 1
    slots = ((t["ts"].map(lambda x: x.value) - b.open_ns) // 60_000_000_000 + 1).to_numpy()
    for r, m in zip(t.itertuples(index=False), slots):
        while nxt < m and nxt <= b.n_slots:
            rows.append(b.finalize(nxt))
            nxt += 1
        b.add_trade(
            TradeIn(
                r.ts,
                r.price,
                int(r.size),
                r.exchange,
                tuple(r.conditions),
                None if pd.isna(r.trade_id) else int(r.trade_id),
                r.tape,
            )
        )
    while nxt <= b.n_slots:
        rows.append(b.finalize(nxt))
        nxt += 1
    live = pd.DataFrame(rows)[BAR_SCHEMA.names]
    diffs = []
    for c in BAR_SCHEMA.names:
        x, y = batch[c].tolist(), live[c].tolist()
        same = [eq(a, b2) for a, b2 in zip(x, y)]
        if not all(same):
            diffs.append((c, same.index(False), x[same.index(False)], y[same.index(False)]))
    print(d, len(trades), "trades", "IDENTICAL" if not diffs else diffs[:3])
    ok &= not diffs
print("ALL IDENTICAL" if ok else "DIFFERENCES")
