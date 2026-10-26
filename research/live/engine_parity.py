# ruff: noqa
"""Live engine parity (integrity): stream stored sessions through LiveSession + ShadowEngine and
require records identical to the batch replay of the same days (equality only; no performance)."""

import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from tournament.features.pipeline import carry_context, run_sessions
from tournament.live.engine import ShadowEngine
from tournament.live.session import LiveSession
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.gate import compare_runs
from tournament.replay.harness import ReplayRun
from tournament.strategies.brt import BRT
from tournament.strategies.fbr import FBR
from tournament.strategies.orb import ORB
from tournament.strategies.vmr import VMR
from tournament.strategies.vpc import VPC

ROOT = Path(__file__).resolve().parents[2]
store = HistoryStore(ROOT / "data" / "history")
START, END = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
PICKS = {date.fromisoformat(a) for a in sys.argv[3:]}
DS, USAGE = "parity", Usage.LIVE
workers = lambda: [VPC(), ORB(), BRT(), FBR(), VMR()]


def messages(df):
    out = []
    for r in df.itertuples(index=False):
        out.append(
            {
                "T": "t",
                "S": "SPY",
                "t": r.ts.isoformat(),
                "p": r.price,
                "s": int(r.size),
                "x": r.exchange,
                "c": list(r.conditions),
                "i": None if pd.isna(r.trade_id) else int(r.trade_id),
                "z": r.tape,
            }
        )
    return out


t0 = time.time()
outs = [o for o in run_sessions(store, USAGE, START, END) if o.day.session_date in PICKS]
print("batch sessions built", len(outs), round(time.time() - t0), "s", flush=True)
prior, base = carry_context(store, USAGE, outs[0].day.session_date)
same_ctx = prior == outs[0].prior and np.array_equal(
    prior and base, outs[0].rvol_baseline, equal_nan=True
)
print("carry_context equals pipeline context:", same_ctx, flush=True)
ok = same_ctx
cfg = EligibilityConfig()
for out in outs:
    d = out.day.session_date
    ref = ReplayRun(workers(), USAGE, run_id="batch", dataset_id=DS).run_outputs([out])
    s = LiveSession(
        out.day,
        cfg,
        usage=USAGE,
        finalize_delay=pd.Timedelta(seconds=2).to_pytimedelta(),
        halt_codes=frozenset({"H", "P"}),
        resume_codes=frozenset({"T"}),
        stale_after=pd.Timedelta(hours=1).to_pytimedelta(),
    )
    eng = ShadowEngine(
        s, workers(), out.prior, out.rvol_baseline, usage=USAGE, run_id="live", dataset_id=DS
    )
    open_ts = pd.Timestamp(out.day.open_utc)
    s.on_connected(pd.Timestamp(out.day.extended_start_utc) - pd.Timedelta(minutes=1))
    pm = store.read("spy_trades_premarket", d).to_pandas().sort_values("ts", kind="stable")
    s.on_messages(messages(pm), open_ts - pd.Timedelta(seconds=1))
    t = store.read("spy_trades", d).to_pandas().sort_values("ts", kind="stable")
    slot = (t["ts"].map(lambda x: x.value) - s.builder.open_ns) // 60_000_000_000 + 1
    for m, g in t.groupby(slot, sort=True):
        s.on_messages(messages(g), s.builder.bar_end(int(m)) - pd.Timedelta(seconds=0.5))
    s.tick(pd.Timestamp(out.day.close_utc) + pd.Timedelta(seconds=3))
    res = eng.result()
    chk = compare_runs(ref, res)
    lat = np.array(eng.latencies)
    print(
        d,
        "slots",
        s.builder.finalized,
        "stepped",
        eng.stepped,
        "error",
        eng.error,
        "records",
        chk.checked,
        "IDENTICAL" if chk.passed else chk.failures[:2],
        "step p50/max ms",
        round(np.percentile(lat, 50) * 1000),
        round(lat.max() * 1000),
        flush=True,
    )
    ok &= chk.passed and eng.error is None and s.builder.finalized == s.builder.n_slots
print("ALL IDENTICAL" if ok else "DIFFERENCES")
