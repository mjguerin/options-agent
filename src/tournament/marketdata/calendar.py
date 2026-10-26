"""Exchange session calendar with canonical UTC times (MD-06)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tournament.appconfig import app_config


@dataclass(frozen=True)
class SessionDay:
    session_date: date
    open_utc: datetime
    close_utc: datetime

    @property
    def early_close(self) -> bool:
        ex = app_config().exchange
        return self.close_utc.astimezone(ex.tz).time() < ex.close_time

    @property
    def extended_start_utc(self) -> datetime:
        """The start of pre-market trading (04:00 exchange time unless configured otherwise)."""
        ex = app_config().exchange
        return datetime.combine(self.session_date, ex.extended_start_time, ex.tz).astimezone(
            self.open_utc.tzinfo
        )

    @property
    def extended_end_utc(self) -> datetime:
        """The configured hours after the close (20:00 on a normal day)."""
        return self.close_utc + timedelta(hours=app_config().exchange.after_hours_hours)


def session_from_exchange_times(
    session_date: date, open_local: time, close_local: time
) -> SessionDay:
    """Build a session from exchange-local wall times (how Alpaca's calendar reports them)."""
    utc = ZoneInfo("UTC")
    tz = app_config().exchange.tz
    return SessionDay(
        session_date=session_date,
        open_utc=datetime.combine(session_date, open_local, tz).astimezone(utc),
        close_utc=datetime.combine(session_date, close_local, tz).astimezone(utc),
    )
