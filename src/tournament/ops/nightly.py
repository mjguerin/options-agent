"""`tournament ops nightly`: after the close, bring the history store up to date and say so.

Downloads every missing session up to today (days already on disk are skipped), then checks that
each session in the store has every dataset. A summary goes to data/ops/. A non-zero exit means
something is missing, which the alert bot will report once it exists.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from tournament.appconfig import app_config
from tournament.marketdata.history import DATASETS

log = logging.getLogger(__name__)


def coverage(store, sessions: list[date]) -> dict[str, list[str]]:
    """Datasets missing per session date."""
    out = {}
    for d in sessions:
        gone = [name for name in DATASETS if not store.has(name, d)]
        if gone:
            out[d.isoformat()] = gone
    return out


def row_count(store, name: str, d: date) -> int:
    import pyarrow.parquet as pq

    return pq.ParquetFile(store.path(name, d)).metadata.num_rows


def quality(store, sessions: list[date], q) -> dict[str, list[str]]:
    """Datasets that exist but don't look like a real session (an empty or truncated download
    still has a file). Checked on the most recent sessions only: older ones were verified when
    they were downloaded."""
    floors = {
        "spy_trades": q.min_spy_trades,
        "spy_bars_1m": q.min_spy_bars,
        "option_contracts": q.min_option_contracts,
        "option_bars_1m": q.min_option_bars,
    }
    out: dict[str, list[str]] = {}
    for d in sessions[-q.check_last_n_sessions :]:
        bad = []
        for name, floor in floors.items():
            if store.has(name, d):
                n = row_count(store, name, d)
                if n < floor:
                    bad.append(f"{name}: {n} rows, expected at least {floor}")
        if bad:
            out[d.isoformat()] = bad
    return out


def run_nightly(today: date, data_root: Path) -> int:
    from tournament.adapters.alpaca import data_account_alias
    from tournament.adapters.alpaca_history import AlpacaHistory
    from tournament.features.pipeline import calendar_days
    from tournament.marketdata.history import (
        Downloader,
        SessionGuardError,
        UniverseRule,
        check_outside_session,
        code_commit,
        history_start,
    )
    from tournament.marketdata.store import HistoryStore
    from tournament.secrets import load_alpaca_credentials

    store = HistoryStore(data_root / "history")
    creds = load_alpaca_credentials(data_account_alias())
    cfg = app_config()
    started = datetime.now()
    todays = AlpacaHistory(creds).calendar(today, today)
    if todays and todays[0].session_date == today:
        ready = pd.Timestamp(todays[0].close_utc) + pd.Timedelta(
            minutes=cfg.nightly.after_close_min
        )
        if pd.Timestamp.now(tz="UTC") < ready:
            log.info("too early: today's data is not downloaded before %s", ready)
            return 0
    error = None
    try:
        Downloader(
            AlpacaHistory(creds),
            store,
            UniverseRule.from_config(),
            commit=code_commit(Path(__file__).resolve().parents[3]),
        ).run(history_start(), today, before_each_day=check_outside_session)
    except SessionGuardError as exc:
        error = f"guard: {exc}"
    except Exception as exc:  # noqa: BLE001 - reported in the summary and the exit code
        error = f"{type(exc).__name__}: {exc}"
        log.exception("nightly download failed")
    sessions = [d.session_date for d in calendar_days(store) if d.session_date <= today]
    missing = coverage(store, sessions)
    thin = quality(store, sessions, cfg.nightly.quality)
    summary = {
        "date": today.isoformat(),
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "sessions_in_store": len(sessions),
        "missing": missing,
        "quality_issues": thin,
        "error": error,
    }
    out = data_root / "ops"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"nightly_{today.isoformat()}.json").write_text(json.dumps(summary, indent=1))
    log.info("nightly: %d sessions, %d with gaps, error=%s", len(sessions), len(missing), error)
    return 0 if not missing and not thin and error is None else 1
