"""Exchange session calendar with canonical UTC times (MD-06)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)


@dataclass(frozen=True)
class SessionDay:
    session_date: date
    open_utc: datetime
    close_utc: datetime

    @property
    def early_close(self) -> bool:
        return self.close_utc.astimezone(EXCHANGE_TZ).time() < REGULAR_CLOSE

    @property
    def extended_start_utc(self) -> datetime:
        """04:00 exchange time, the start of pre-market trading."""
        return datetime.combine(self.session_date, time(4, 0), EXCHANGE_TZ).astimezone(
            self.open_utc.tzinfo
        )

    @property
    def extended_end_utc(self) -> datetime:
        """Four hours after the close (20:00 on a normal day)."""
        return self.close_utc + timedelta(hours=4)


def session_from_exchange_times(
    session_date: date, open_local: time, close_local: time
) -> SessionDay:
    """Build a session from exchange-local wall times (how Alpaca's calendar reports them)."""
    utc = ZoneInfo("UTC")
    return SessionDay(
        session_date=session_date,
        open_utc=datetime.combine(session_date, open_local, EXCHANGE_TZ).astimezone(utc),
        close_utc=datetime.combine(session_date, close_local, EXCHANGE_TZ).astimezone(utc),
    )
