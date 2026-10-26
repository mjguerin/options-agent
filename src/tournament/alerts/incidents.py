"""The engine's side: best-effort incident writing (BRD 10.5).

The engine must never stop or slow down because an alert could not be written, so every call
swallows its own failure, counts it and logs it. Without a reachable database the engine runs
exactly as before and the bot's watchdogs still notice it.
"""

from __future__ import annotations

import logging

import pandas as pd

from tournament.alerts.config import AlertsConfig
from tournament.alerts.store import AlertStore

log = logging.getLogger(__name__)


class Incidents:
    def __init__(self, store: AlertStore | None, component: str = "engine") -> None:
        self.store, self.component, self.failures = store, component, 0

    @classmethod
    def from_config(cls, cfg: AlertsConfig, component: str = "engine") -> Incidents:
        try:
            from tournament.orders.store import default_engine

            return cls(AlertStore(default_engine(), cfg), component)
        except Exception:  # noqa: BLE001
            log.exception("alerts unavailable: the session runs without incident records")
            return cls(None, component)

    def _do(self, fn, *args, **kw):
        if self.store is None:
            return None
        try:
            return fn(*args, **kw)
        except Exception:  # noqa: BLE001
            self.failures += 1
            log.exception("could not write an incident")
            return None

    def open(
        self,
        now: pd.Timestamp,
        kind: str,
        detail: str = "",
        account: str | None = None,
        key: str | None = None,
    ):
        return self._do(
            self.store.open if self.store else None, now, kind, self.component, detail, account, key
        )

    def resolve(
        self, now: pd.Timestamp, kind: str, account: str | None = None, key: str | None = None
    ):
        key = key or (f"{kind}:{account}" if account else kind)
        return self._do(self.store.resolve if self.store else None, now, key, kind)

    def notify(self, now: pd.Timestamp, kind: str, detail: str, account: str | None = None):
        return self._do(self.store.notify if self.store else None, now, kind, detail, account)
