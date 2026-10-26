"""Feed health and halts as time intervals (SD-01 interval status, MD-03, AC-08).

An interval is [start, end); an open interval has end None. The feed starts unhealthy until
the first subscription succeeds. A slot is uncertain when any unhealthy interval overlaps
[bar_start, scheduled_finalization_at): a trade of that slot could have been missed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

Interval = list  # [start, end | None, reason]


def _overlaps(intervals: list[Interval], a: pd.Timestamp, b: pd.Timestamp) -> bool:
    return any(s < b and (e is None or e > a) for s, e, _ in intervals)


@dataclass
class FeedHealth:
    stale_after: pd.Timedelta
    unhealthy: list[Interval] = field(
        default_factory=lambda: [[pd.Timestamp.min.tz_localize("UTC"), None, "not_connected"]]
    )
    halts: list[Interval] = field(default_factory=list)
    last_message_at: pd.Timestamp | None = None
    connected: bool = False

    def _open(self, at: pd.Timestamp, reason: str) -> None:
        if not self.unhealthy or self.unhealthy[-1][1] is not None:
            self.unhealthy.append([at, None, reason])

    def _close(self, at: pd.Timestamp) -> None:
        if self.unhealthy and self.unhealthy[-1][1] is None:
            self.unhealthy[-1][1] = at

    def on_connected(self, at: pd.Timestamp) -> None:
        self.connected, self.last_message_at = True, at
        self._close(at)

    def on_disconnected(self, at: pd.Timestamp) -> None:
        self.connected = False
        self._open(at, "disconnected")

    def on_message(self, at: pd.Timestamp) -> None:
        self.last_message_at = at
        if self.connected:
            self._close(at)

    def check_stale(self, now: pd.Timestamp, regular_open: pd.Timestamp) -> None:
        """No message for `stale_after` while connected: the feed may be silently stuck.
        Checked only from the regular open, because thin pre-market trading has normal
        silences (D-48). The interval starts at the last message (or the open), the earliest
        moment input may be missing."""
        if now < regular_open or not self.connected or self.last_message_at is None:
            return
        if now - self.last_message_at > self.stale_after:
            self._open(max(self.last_message_at, regular_open), "stale")

    def on_status(self, code: str, at: pd.Timestamp, halt_codes, resume_codes) -> None:
        open_halt = bool(self.halts) and self.halts[-1][1] is None
        if code in halt_codes and not open_halt:
            self.halts.append([at, None, code])
        elif code in resume_codes and open_halt:
            self.halts[-1][1] = at

    def uncertain(self, a: pd.Timestamp, b: pd.Timestamp) -> bool:
        return _overlaps(self.unhealthy, a, b)

    def halted(self, a: pd.Timestamp, b: pd.Timestamp) -> bool:
        return _overlaps(self.halts, a, b)
