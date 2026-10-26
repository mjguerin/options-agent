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
        engine=None,
    ) -> None:
        self.store, self.cfg, self.runtimes = store, cfg, runtimes
        self.state_file, self.session, self.mode, self.collector = (
            state_file,
            session,
            mode,
            collector,
        )
        self.engine = engine
        self._last_history: pd.Timestamp | None = None
        self._last_poll: pd.Timestamp | None = None
        self._last_state: pd.Timestamp | None = None
        self.applied = 0
        self.seq = 0

    def tick(self, now: pd.Timestamp) -> None:
        every = pd.Timedelta(seconds=self.cfg.state.every_s)
        if self._last_state is None or now - self._last_state >= every:
            self._last_state = now
            self._publish(now)
        hist = pd.Timedelta(seconds=self.cfg.state.history_every_s)
        if self._last_history is None or now - self._last_history >= hist:
            self._last_history = now
            self._append_history(now)
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
                "positions_detail": rt.positions_detail(now),
                "recent_decisions": [
                    {
                        "at": d.at.isoformat() if d.at is not None else None,
                        "candidate_id": d.candidate_id,
                        "accepted": d.accepted,
                        "reasons": list(d.reasons),
                        "contract": d.selection.symbol if d.selection else None,
                        "quantity": d.size.quantity if d.size else None,
                    }
                    for d in rt.decisions[-self.cfg.state.recent_decisions :]
                ],
                "halted": rt.halted,
                "operator_halt": rt.operator_halt,
                "entries_blocked": rt.orders.entries_blocked,
                "positions": rt.store.filled_by_symbol(binding),
                "exit_blind": sorted(rt.orders.blind),
                "pending_orders": len(rt.store.active(binding)),
                "exposure_usd": round(rt.ledger.exposure_usd(binding), 2),
                "realized_pnl_usd": round(rt.realized_pnl(), 2),
                "pnl_view": {
                    k: round(v, 2) if isinstance(v, float) else v
                    for k, v in rt.guard.risk_view(
                        rt.realized_pnl(), rt._open_positions(now)
                    ).items()
                },
                "decisions": len(rt.decisions),
            }
        feeds = {"stock": {"connected": s.health.connected}}
        if self.collector is not None:
            feeds["option"] = {
                "connected": self.collector.feed.health.connected,
                "error": self.collector.error,
                "lag_s": self.collector.feed.book.lag_percentiles(),
            }
        self.seq += 1
        return {
            "seq": self.seq,  # a reader sees stale state as a number that stopped moving
            "at": now.isoformat(),
            "session": s.day.session_date.isoformat(),
            "mode": self.mode,
            "slot": s.builder.finalized,
            "feeds": feeds,
            "accounts": accounts,
            "strategies": self._strategies(),
        }

    def _strategies(self) -> dict:
        """Each worker's latest evaluation: where its state machine is and why it can't act."""
        if self.engine is None:
            return {}
        out = {}
        for w in self.engine.harness.workers:
            r = self.engine.harness.latest_evaluations().get(w.strategy_id)
            out[w.strategy_id] = {
                "version": w.version,
                "config_hash": w.config_hash[:12],
                "slot": r["slot"] if r else None,
                "outcome": r["outcome"] if r else None,
                "setup_state": r["setup_state"] if r else None,
                "readiness": r["readiness_status"] if r else None,
                "blocks": list(r["eligibility_blocks"]) if r else [],
                "reasons": list(r["reason_codes"]) if r else [],
            }
        return out

    def _append_history(self, now: pd.Timestamp) -> None:
        """One line per interval: what the dashboard draws its curves from."""
        try:
            d = self.state_file.parent / "state_history"
            d.mkdir(parents=True, exist_ok=True)
            row = {
                "at": now.isoformat(),
                "accounts": {
                    b: {
                        "realized_pnl_usd": round(rt.realized_pnl(), 2),
                        "unrealized_usd": round(
                            sum(p["unrealized_usd"] or 0.0 for p in rt.positions_detail(now)), 2
                        ),
                        "exposure_usd": round(rt.ledger.exposure_usd(b), 2),
                        "risk_pnl_usd": self._pnl_view(rt, now)["risk_usd"],
                        "unmarked": self._pnl_view(rt, now)["unmarked_positions"],
                    }
                    for b, rt in self.runtimes.items()
                },
            }
            with (d / f"{self.session.day.session_date.isoformat()}.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("could not append the state history")

    def _publish(self, now: pd.Timestamp) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(now), default=str))
            tmp.replace(self.state_file)  # the API never reads a half-written file
        except Exception:  # noqa: BLE001
            log.exception("could not publish the engine state")
