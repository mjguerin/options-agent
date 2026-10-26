"""`tournament alerts run|test|status`: the alert bot as a program."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from tournament.alerts.bot import AlertBot, heartbeat_path
from tournament.alerts.config import AlertsConfig, load_alerts_config
from tournament.alerts.store import AlertStore
from tournament.alerts.telegram import Sender, TelegramSender
from tournament.alerts.watchdog import (
    Finding,
    check_backup,
    check_database,
    check_disk,
    check_engine,
    check_nightly,
    check_supervisor,
    last_weekday_due,
    read_heartbeat,
    supervisor_status,
)
from tournament.appconfig import app_config
from tournament.secrets import load_telegram_credentials

log = logging.getLogger(__name__)


def build_watches(cfg: AlertsConfig, repo: Path, engine, today_session) -> list:
    """The checks the bot runs itself. `today_session(now)` returns the SessionDay or None."""
    app = app_config()
    w = cfg.watchdog
    hb = heartbeat_path(repo, cfg.heartbeat.file)

    def engine_watch(now: pd.Timestamp) -> list[Finding]:
        from tournament.ops.schedule import start_moment

        day = today_session(now)
        start = start_moment(day, app) if day else None
        close = pd.Timestamp(day.close_utc) if day else None
        return check_engine(
            now, day.session_date if day else None, start, close, read_heartbeat(hb), w
        )

    def nightly_watch(now: pd.Timestamp) -> list[Finding]:
        due = last_weekday_due(
            now, app.exchange.tz, app.nightly.start_time, w.nightly_deadline_after_s
        )
        return check_nightly(due, repo / "data" / "ops", w.nightly_first_check_date)

    return [
        engine_watch,
        nightly_watch,
        lambda now: check_backup(now, Path(w.backup_log).expanduser(), w),
        lambda now: check_supervisor(supervisor_status(repo / w.supervisor_conf, w.supervisorctl)),
        lambda now: check_disk(repo / w.disk_path, w),
        lambda now: check_database(engine),
    ]


def calendar_provider():
    """Today's session from the exchange calendar, fetched once a day; if the calendar can't be
    reached, a weekday is assumed to be a session so a missing engine is still noticed."""
    from datetime import time

    from tournament.adapters.alpaca import data_account_alias
    from tournament.adapters.alpaca_history import AlpacaHistory
    from tournament.marketdata.calendar import session_from_exchange_times

    cache: dict = {}
    tz = app_config().exchange.tz

    def provider(now: pd.Timestamp):
        today = now.tz_convert(tz).date()
        if today not in cache:
            try:
                from tournament.secrets import load_alpaca_credentials

                days = AlpacaHistory(load_alpaca_credentials(data_account_alias())).calendar(
                    today, today
                )
                cache[today] = days[0] if days and days[0].session_date == today else None
            except Exception:  # noqa: BLE001
                log.warning("calendar unavailable; assuming a weekday session")
                cache[today] = (
                    session_from_exchange_times(today, time(9, 30), time(16, 0))
                    if today.weekday() < 5
                    else None
                )
        return cache[today]

    return provider


def run_bot(repo: Path, sender: Sender | None = None, until: pd.Timestamp | None = None) -> int:
    from tournament.orders.store import default_engine

    cfg = load_alerts_config()
    sender = sender or TelegramSender(load_telegram_credentials(), cfg.telegram)
    engine = default_engine()
    bot = AlertBot(
        AlertStore(engine, cfg), sender, cfg, build_watches(cfg, repo, engine, calendar_provider())
    )
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(now=True))
    log.info("alert bot running (%s)", cfg.version)
    while not stop["now"]:
        now = pd.Timestamp(datetime.now().astimezone()).tz_convert("UTC")
        if until is not None and now >= until:
            break
        try:
            bot.step(now)
        except Exception:  # noqa: BLE001 - the bot outlives any single failure
            log.exception("alert bot step failed")
        time.sleep(cfg.delivery.poll_s)
    return 0
