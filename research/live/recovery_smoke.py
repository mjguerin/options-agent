# ruff: noqa
"""Recovery smoke (integrity, no strategy behaviour): replay today's real journal through the
live session and decision engine twice. Reports speed, errors and that both replays agree.
Reads the journal; writes nothing."""

import sys, time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tournament.features.pipeline import carry_context
from tournament.live.engine import ShadowEngine
from tournament.live.recovery import previous_runs, replay_journals
from tournament.live.runner import all_workers, load_feed_config
from tournament.live.session import LiveSession
from tournament.marketdata.calendar import SessionDay
from tournament.marketdata.eligibility import EligibilityConfig, Usage
from tournament.marketdata.store import HistoryStore
from tournament.replay.gate import compare_runs

ROOT = Path(__file__).resolve().parents[2]
day_dir = ROOT / "data" / "live" / sys.argv[1]
d = date.fromisoformat(sys.argv[1])
cal = pd.read_parquet(ROOT / "data/history/calendar.parquet")
r = (
    next(x for x in cal.itertuples() if x.session_date == d)
    if (cal.session_date == d).any()
    else None
)
if r is None:  # today is not in the store's calendar yet: the regular session times
    from tournament.marketdata.calendar import session_from_exchange_times
    from datetime import time as t

    day = session_from_exchange_times(d, t(9, 30), t(16, 0))
else:
    day = SessionDay(d, r.open_utc.to_pydatetime(), r.close_utc.to_pydatetime())
store = HistoryStore(ROOT / "data" / "history")
prior, base = carry_context(store, Usage.LIVE, date.fromisoformat(sys.argv[2]))
fc = load_feed_config()
runs = previous_runs(day_dir, "shadow")
print("journals:", [p.name for p in runs], flush=True)


def once():
    s = LiveSession(
        day,
        EligibilityConfig(),
        usage=Usage.LIVE,
        finalize_delay=timedelta(seconds=2),
        stale_after=timedelta(seconds=fc["stale_after_s"]),
        halt_codes=frozenset(fc["halt_status_codes"]),
        resume_codes=frozenset(fc["resume_status_codes"]),
        informational_codes=frozenset(fc["informational_status_codes"]),
        unknown_status_hold=timedelta(seconds=fc["unknown_status_hold_s"]),
    )
    e = ShadowEngine(s, all_workers(), prior, base, usage=Usage.LIVE, run_id=f"live-{d}-shadow")
    t0 = time.time()
    info = replay_journals(s, runs)
    return s, e, info, time.time() - t0


a = once()
b = once()
for s, e, info, secs in (a, b):
    print(
        "frames",
        info["frames"],
        "slots",
        s.builder.finalized,
        "engine error",
        e.error,
        "stepped",
        e.stepped,
        f"{secs:.1f}s",
        flush=True,
    )
chk = compare_runs(a[1].result(), b[1].result())
print("two replays agree:", chk.passed, "records compared:", chk.checked)
