"""`tournament engine daily`: the program Supervisor starts before the open."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from tournament.appconfig import app_config
from tournament.ops.schedule import Action, engine_plan

log = logging.getLogger(__name__)
WAIT_SLICE_S = 30.0  # how often a waiting engine wakes to re-check the clock


def run_daily(
    repo_root: Path,
    out_root: Path,
    now_fn: Callable[[], pd.Timestamp] = lambda: pd.Timestamp(datetime.now().astimezone()),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Returns the process exit code: 0 for a normal run or a deliberate skip, non-zero for a
    failure, which Supervisor answers with a restart (up to the configured attempts)."""
    from tournament.accounts.manifest import load_manifest
    from tournament.adapters.alpaca import data_account_alias
    from tournament.adapters.alpaca_history import AlpacaHistory
    from tournament.live.runner import PreflightFailed, run_session
    from tournament.marketdata.history import history_start
    from tournament.marketdata.store import HistoryStore
    from tournament.secrets import load_alpaca_credentials

    cfg = app_config()
    creds = load_alpaca_credentials(data_account_alias())
    today = now_fn().tz_convert(cfg.exchange.tz).date()
    days = AlpacaHistory(creds).calendar(today, today)
    day = days[0] if days and days[0].session_date == today else None
    while True:
        plan = engine_plan(now_fn().tz_convert("UTC"), day, cfg)
        if plan.action is not Action.WAIT:
            break
        log.info("waiting %.0f s for %s", plan.wait_s, plan.start_at)
        sleep(min(plan.wait_s, WAIT_SLICE_S))
    if plan.action is not Action.RUN:
        log.info("nothing to run today: %s", plan.action.value)
        sleep(cfg.engine.startsecs + 2)  # stay up long enough for Supervisor to count a start
        return 0
    manifest = load_manifest(repo_root / cfg.engine.manifest) if cfg.engine.manifest else None
    store = HistoryStore(out_root.parent / "history")
    log.info("starting the %s session, history from %s", day.session_date, history_start())
    try:
        run_session(day, "shadow", creds, out_root, store, manifest=manifest)
    except PreflightFailed as exc:
        log.error("%s", exc)
        return 3  # a configuration problem: a restart won't fix it, but the alert will say so
    return 0
