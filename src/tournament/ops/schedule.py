"""When the daily engine runs (BRD 9.3, 10.2). Pure functions of the clock and the calendar.

launchd starts the Supervisor program a few minutes early every weekday. The engine then decides
for itself, from the exchange calendar: not a session, nothing to do; before the start time, wait
for it (so a launchd that fires early or late changes nothing); inside the window, run; after the
close, do nothing. A crashed engine restarted by Supervisor lands in the window and runs again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from tournament.appconfig import AppConfig
from tournament.marketdata.calendar import SessionDay


class Action(StrEnum):
    SKIP_NOT_A_SESSION = "SKIP_NOT_A_SESSION"
    WAIT = "WAIT"
    RUN = "RUN"
    SKIP_TOO_LATE = "SKIP_TOO_LATE"


@dataclass(frozen=True)
class Plan:
    action: Action
    start_at: pd.Timestamp | None = None
    wait_s: float = 0.0


def start_moment(day: SessionDay, cfg: AppConfig) -> pd.Timestamp:
    ex = cfg.exchange
    h, m = cfg.engine.start_time.split(":")
    local = pd.Timestamp(day.session_date).tz_localize(ex.tz) + pd.Timedelta(
        hours=int(h), minutes=int(m)
    )
    return local.tz_convert("UTC")


def engine_plan(now: pd.Timestamp, day: SessionDay | None, cfg: AppConfig) -> Plan:
    if day is None:
        return Plan(Action.SKIP_NOT_A_SESSION)
    start = start_moment(day, cfg)
    end = pd.Timestamp(day.close_utc) + pd.Timedelta(seconds=cfg.engine.late_after_close_s)
    if now >= end:
        return Plan(Action.SKIP_TOO_LATE, start)
    if now < start:
        return Plan(Action.WAIT, start, (start - now).total_seconds())
    return Plan(Action.RUN, start)
