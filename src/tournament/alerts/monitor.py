"""The engine's own checks, run once a second (BRD 10.5): turn what the session sees into
incidents, and write the heartbeat the bot's watchdog reads.

Reads the session, the option collector, the decision engine and the accounts; changes nothing in
them. A feed must stay down for `feed_down_grace_s` before it is an incident, so the first seconds
of a connect, or a one-second reconnect, say nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from tournament.alerts.config import AlertsConfig
from tournament.alerts.incidents import Incidents

log = logging.getLogger(__name__)


class SessionMonitor:
    def __init__(
        self,
        incidents: Incidents,
        cfg: AlertsConfig,
        session,
        heartbeat_file: Path,
        mode: str,
        collector=None,
        engine=None,
        accounts=None,
    ) -> None:
        self.inc, self.cfg, self.session, self.mode = incidents, cfg, session, mode
        self.collector, self.engine, self.accounts = collector, engine, accounts
        self.heartbeat_file = heartbeat_file
        self._last: pd.Timestamp | None = None
        self._last_beat: pd.Timestamp | None = None
        self._down_since: dict[str, pd.Timestamp | None] = {"stock": None, "option": None}
        self._seen_errors = 0
        self._seen_quarantined = 0
        flat = pd.Timedelta(minutes=cfg.monitor.flat_deadline_min_before_close)
        self.flat_deadline = pd.Timestamp(session.day.close_utc) - flat

    def tick(self, now: pd.Timestamp) -> None:
        if self._last is not None and (now - self._last).total_seconds() < 1.0:
            return
        self._last = now
        self._heartbeat(now)
        self._feed(now, "stock", "STOCK_FEED_DOWN", self.session.health)
        if self.collector is not None:
            self._option_feed(now)
        self._engine(now)
        self._accounts(now)
        self._conditions(now)

    # ---- the heartbeat the bot's watchdog reads ----------------------------------------------
    def _heartbeat(self, now: pd.Timestamp) -> None:
        every = pd.Timedelta(seconds=self.cfg.heartbeat.every_s)
        if self._last_beat is not None and now - self._last_beat < every:
            return
        self._last_beat = now
        body = {
            "at": now.isoformat(),
            "session": self.session.day.session_date.isoformat(),
            "mode": self.mode,
            "slot": self.session.builder.finalized,
        }
        try:
            self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heartbeat_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(body))
            tmp.replace(self.heartbeat_file)  # the bot never reads a half-written file
        except OSError:
            log.exception("could not write the heartbeat")

    # ---- feeds -----------------------------------------------------------------------------
    def _feed(self, now, name: str, kind: str, health) -> None:
        reason = None
        if not health.connected:
            reason = "not connected"
        elif health.unhealthy and health.unhealthy[-1][1] is None:
            reason = health.unhealthy[-1][2]
        if reason is None:
            if self._down_since[name] is not None:
                self._down_since[name] = None
                self.inc.resolve(now, kind)
            return
        if self._down_since[name] is None:
            self._down_since[name] = now
        if (now - self._down_since[name]).total_seconds() >= self.cfg.monitor.feed_down_grace_s:
            self.inc.open(now, kind, reason)

    def _option_feed(self, now) -> None:
        if self.collector.error:
            self.inc.open(now, "OPTION_FEED_FAILED", self.collector.error)
            return
        self._feed(now, "option", "OPTION_FEED_DOWN", self.collector.feed.health)

    # ---- the decision engine and the accounts ---------------------------------------------------
    def _engine(self, now) -> None:
        if self.engine is not None and self.engine.error:
            self.inc.open(now, "ENGINE_ERROR", self.engine.error)

    def _accounts(self, now) -> None:
        a = self.accounts
        if a is None:
            return
        for err in a.errors[self._seen_errors :]:
            self.inc.open(
                now, "ACCOUNT_ERROR", err, key=f"ACCOUNT_ERROR:{len(a.errors)}:{err[:40]}"
            )
        self._seen_errors = len(a.errors)
        for binding, rt in a.router.runtimes.items():
            if rt.halted == "DAILY_LIMIT":
                loss = rt.guard.update(now, rt.realized_pnl(), []).loss
                self.inc.open(
                    now,
                    "DAILY_LIMIT_HALT",
                    f"${loss:,.0f} of ${rt.risk.daily_loss.limit_usd:,.0f}",
                    binding,
                )
            if rt.orders.entries_blocked:
                self.inc.open(now, "ENTRIES_BLOCKED", rt.orders.entries_blocked, binding)
            else:
                self.inc.resolve(now, "ENTRIES_BLOCKED", binding)
            held = rt.store.filled_by_symbol(binding)
            if now >= self.flat_deadline and held:
                self.inc.open(
                    now,
                    "RESIDUAL_EXPOSURE",
                    ", ".join(f"{q} x {s}" for s, q in held.items()),
                    binding,
                )
            elif not held:
                self.inc.resolve(now, "RESIDUAL_EXPOSURE", binding)

    def _conditions(self, now) -> None:
        """D-36: unknown condition codes are monitored and alerted, however rare."""
        total = sum(b.get("quarantined_trades", 0) for b in self.session.bars)
        if total > self._seen_quarantined:
            n = total - self._seen_quarantined
            self._seen_quarantined = total
            self.inc.notify(
                now, "UNKNOWN_CONDITION", f"{n} trade(s) quarantined, {total} so far today"
            )

    # ---- end of session ------------------------------------------------------------------------
    def finish(self, now: pd.Timestamp) -> None:
        if not self.cfg.delivery.session_summary:
            return
        bars = pd.DataFrame(self.session.bars)
        counts = bars["interval_status"].value_counts().to_dict() if len(bars) else {}
        parts = [
            f"Session {self.session.day.session_date} ({self.mode}): "
            f"{self.session.builder.finalized} slots "
            + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        ]
        if self.accounts is not None:
            s = self.accounts.summary()["accounts"]
            decided = sum(v["decisions"] for v in s.values())
            pnl = sum(v["realized_pnl_usd"] for v in s.values())
            parts.append(
                f"{decided} candidate decision(s); modeled realized P&L ${pnl:,.0f} "
                "across five accounts"
            )
        if self.engine is not None and self.engine.error:
            parts.append(f"decision engine error: {self.engine.error}")
        self.inc.notify(now, "SESSION_SUMMARY", ". ".join(parts) + ".")
