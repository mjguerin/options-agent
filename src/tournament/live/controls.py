"""The engine's side of the control API (BRD 14).

Once a second the engine looks in the command queue, applies what is there to the account
runtimes, and records the result and an audit line. Every few seconds it publishes its state
(feeds, accounts, positions) to a file the API and the dashboard read. The engine is the only
thing that touches orders: the API can only ask.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from tournament.api.config import ApiConfig
from tournament.api.store import APPLIED, REJECTED, Command, ControlStore

log = logging.getLogger(__name__)


class ControlPlane:
    def __init__(
        self,
        store: ControlStore | None,
        cfg: ApiConfig,
        runtimes: dict,
        state_file: Path,
        session,
        mode: str,
        collector=None,
    ) -> None:
        self.store, self.cfg, self.runtimes = store, cfg, runtimes
        self.state_file, self.session, self.mode, self.collector = (
            state_file,
            session,
            mode,
            collector,
        )
        self._last_poll: pd.Timestamp | None = None
        self._last_state: pd.Timestamp | None = None
        self.applied = 0

    def tick(self, now: pd.Timestamp) -> None:
        every = pd.Timedelta(seconds=self.cfg.state.every_s)
        if self._last_state is None or now - self._last_state >= every:
            self._last_state = now
            self._publish(now)
        poll = pd.Timedelta(seconds=self.cfg.commands.poll_s)
        if self.store is not None and (self._last_poll is None or now - self._last_poll >= poll):
            self._last_poll = now
            self._poll(now)

    # ---- commands ---------------------------------------------------------------------------
    def _poll(self, now: pd.Timestamp) -> None:
        try:
            commands = self.store.pending(now)
        except Exception:  # noqa: BLE001 - a database blip must not stop the engine
            log.exception("could not read the command queue")
            return
        for c in commands:
            try:
                status, result = self.apply(now, c)
            except Exception as exc:  # noqa: BLE001 - recorded as a refusal, never a crash
                log.exception("control %s failed", c.command)
                status, result = REJECTED, {"error": f"{type(exc).__name__}: {exc}"}
            self.store.finish(now, c.id, status, result)
            self.store.audit(
                now, c.actor, status, json.dumps(result, default=str)[:500], None,
                c.command, c.scope, c.account, c.id,
            )  # fmt: skip
            self.applied += 1

    def targets(self, c: Command) -> list[str]:
        if c.scope == "global":
            return sorted(self.runtimes)
        return [c.account] if c.account in self.runtimes else []

    def apply(self, now: pd.Timestamp, c: Command) -> tuple[str, dict]:
        names = self.targets(c)
        if not names:
            return REJECTED, {"error": f"no such account {c.account!r} in this session"}
        out: dict = {}
        if c.command == "PAUSE_ENTRIES":
            out = {n: self.runtimes[n].operator_pause(now) for n in names}
        elif c.command == "RESUME_ENTRIES":
            for n in names:
                ok, why = self.runtimes[n].operator_resume(now)
                out[n] = {"resumed": ok, "blocked_by": why}
            if not any(v["resumed"] for v in out.values()):
                return REJECTED, out
        elif c.command == "CANCEL_PENDING":
            out = {n: {"cancelled_entries": self.runtimes[n].cancel_pending(now)} for n in names}
        elif c.command == "CLOSE_POSITION":
            done = self.runtimes[names[0]].close_position(now, c.symbol)
            if not done:
                return REJECTED, {"error": f"{names[0]} holds no {c.symbol}"}
            out = {names[0]: {"exit_placed": c.symbol}}
        elif c.command in ("FLATTEN_ACCOUNT", "EMERGENCY"):
            out = {n: self.runtimes[n].flatten(now, f"OPERATOR_{c.command}") for n in names}
        else:
            return REJECTED, {"error": f"the engine does not know {c.command}"}
        return APPLIED, out

    # ---- published state ------------------------------------------------------------------------
    def snapshot(self, now: pd.Timestamp) -> dict:
        s = self.session
        accounts = {}
        for binding, rt in self.runtimes.items():
            accounts[binding] = {
                "strategy": rt.strategy_id,
                "halted": rt.halted,
                "operator_halt": rt.operator_halt,
                "entries_blocked": rt.orders.entries_blocked,
                "positions": rt.store.filled_by_symbol(binding),
                "pending_orders": len(rt.store.active(binding)),
                "exposure_usd": round(rt.ledger.exposure_usd(binding), 2),
                "realized_pnl_usd": round(rt.realized_pnl(), 2),
                "decisions": len(rt.decisions),
            }
        feeds = {"stock": {"connected": s.health.connected}}
        if self.collector is not None:
            feeds["option"] = {
                "connected": self.collector.feed.health.connected,
                "error": self.collector.error,
                "lag_s": self.collector.feed.book.lag_percentiles(),
            }
        return {
            "at": now.isoformat(),
            "session": s.day.session_date.isoformat(),
            "mode": self.mode,
            "slot": s.builder.finalized,
            "feeds": feeds,
            "accounts": accounts,
        }

    def _publish(self, now: pd.Timestamp) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(now), default=str))
            tmp.replace(self.state_file)  # the API never reads a half-written file
        except Exception:  # noqa: BLE001
            log.exception("could not publish the engine state")
