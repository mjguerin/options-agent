"""The alert bot: a separate program (BRD 9, AG-09) that delivers and watches.

Each step: run the watchdog checks when they are due, repeat critical incidents that stay open,
then deliver what is waiting, most severe first. Delivery is rate limited, retried with backoff,
and expires: an alert that cannot be sent for `expire_after_s` is dropped and said so in the next
message, so an hour-old outage doesn't arrive as if it were happening now.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from tournament.alerts.config import RANK, AlertsConfig
from tournament.alerts.store import AlertStore
from tournament.alerts.telegram import Sender
from tournament.alerts.watchdog import Finding

log = logging.getLogger(__name__)
Watch = Callable[[pd.Timestamp], list[Finding]]


@dataclass
class AlertBot:
    store: AlertStore
    sender: Sender
    cfg: AlertsConfig
    watches: list[Watch] = field(default_factory=list)
    _last_watch: pd.Timestamp | None = None
    stats: dict = field(
        default_factory=lambda: {"sent": 0, "failed": 0, "expired": 0, "dropped": 0}
    )

    # ---- watchdogs ----------------------------------------------------------------------
    def _watch(self, now: pd.Timestamp) -> None:
        w = self.cfg.watchdog
        if (
            self._last_watch is not None
            and (now - self._last_watch).total_seconds() < w.check_every_s
        ):
            return
        self._last_watch = now
        for watch in self.watches:
            try:
                findings = watch(now)
            except Exception as exc:  # noqa: BLE001 - a broken check must not stop the others
                log.exception("watchdog failed")
                findings = []
                self.stats["watch_errors"] = self.stats.get("watch_errors", 0) + 1
                del exc
            for f in findings:
                if f.active:
                    self.store.open(now, f.kind, "watchdog", f.detail, key=f.key)
                else:
                    self.store.resolve(now, f.key, f.kind)

    def _repeats(self, now: pd.Timestamp) -> None:
        every = pd.Timedelta(seconds=self.cfg.delivery.repeat_open_critical_s)
        for inc in self.store.open_incidents():
            last = inc.last_alert_at or inc.opened_at
            if inc.severity == "critical" and now - last >= every:
                self.store.repeat(now, inc)

    # ---- delivery -----------------------------------------------------------------------
    def _deliver(self, now: pd.Timestamp) -> None:
        d = self.cfg.delivery
        room = d.max_per_minute - self.store.sent_since(now - pd.Timedelta(seconds=60))
        if room <= 0:
            return
        expired = 0
        for o in self.store.due(now, room + 50):
            if (now - o.created_at).total_seconds() > d.expire_after_s:
                self.store.drop(o.id, "EXPIRED")
                expired += 1
                continue
            if RANK[o.severity] < RANK[d.min_severity]:
                self.store.drop(o.id, "BELOW_MIN_SEVERITY")
                self.stats["dropped"] += 1
                continue
            if room <= 0:
                break
            res = self.sender.send(o.text)
            if res.ok:
                self.store.mark_sent(now, o.id)
                self.stats["sent"] += 1
                room -= 1
            else:
                backoff = d.retry_backoff_s[min(o.attempts, len(d.retry_backoff_s) - 1)]
                wait = max(backoff, res.retry_after_s or 0)
                self.store.mark_failed(
                    o.id, o.attempts + 1, now + pd.Timedelta(seconds=wait), res.error or ""
                )
                self.stats["failed"] += 1
                break  # the channel is unwell: stop and try again after the backoff
        if expired:
            self.stats["expired"] += expired
            self.store.note(
                now,
                "warning",
                f"{expired} alert(s) could not be delivered in time and were dropped.",
            )

    def step(self, now: pd.Timestamp) -> dict:
        self._watch(now)
        self._repeats(now)
        self._deliver(now)
        return dict(self.stats)


def heartbeat_path(repo: Path, rel: str) -> Path:
    p = Path(rel).expanduser()
    return p if p.is_absolute() else repo / p
