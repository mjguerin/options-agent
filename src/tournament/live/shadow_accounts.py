"""Five modeled paper accounts for a shadow session (BRD 5.3 shadow, FR-01).

Each strategy gets its own account runtime, order manager, ledger claim and modeled broker, all
over one session-local SQLite file, so simulated orders never mix with real paper orders in
PostgreSQL. Candidates and reference exits reach the right account through the router. The
modeled broker fills from the live option quotes (orders/sim.py). Nothing here can reach a real
broker: the broker object is the simulation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from tournament.accounts.reservations import ReservationLedger
from tournament.accounts.runtime import AccountRouter, AccountRuntime
from tournament.db import metadata
from tournament.live.options_runner import OptionsCollector
from tournament.marketdata.calendar import SessionDay
from tournament.orders.config import load_orders_config
from tournament.orders.manager import OrderManager
from tournament.orders.sim import SimBroker
from tournament.orders.store import IntentStore
from tournament.risk.config import load_risk_config

log = logging.getLogger(__name__)


class ShadowAccounts:
    def __init__(
        self,
        day: SessionDay,
        collector: OptionsCollector,
        strategies: list[str],
        starting_balance: float,
        cutoff_min_before_close: int,
        db_path: Path,
        tick_every_s: float,
    ) -> None:
        self.risk, self.orders_cfg = load_risk_config(), load_orders_config()
        self.collector, self.tick_every = collector, pd.Timedelta(seconds=tick_every_s)
        self.engine = create_engine(f"sqlite:///{db_path}")
        metadata.create_all(self.engine)
        self.store = IntentStore(self.engine)
        cutoff = pd.Timestamp(day.close_utc) - pd.Timedelta(minutes=cutoff_min_before_close)
        mult = self.risk.sizing.contract_multiplier
        self.errors: list[str] = []
        self.brokers: dict[str, SimBroker] = {}
        runtimes = {}
        for sid in strategies:
            binding = f"acct:{sid}"
            sim = SimBroker(self._touch, self.orders_cfg.simulation, starting_balance, mult)
            self.brokers[binding] = sim
            runtimes[binding] = AccountRuntime(
                binding,
                sid,
                self.risk,
                OrderManager(binding, sim, self.store, self.orders_cfg),
                self.store,
                ReservationLedger(self.engine, self.risk.limits, mult),
                collector.feed,
                sim,
                collector.universe.contracts,
                day.session_date,
                starting_balance,
                starting_balance,
                cutoff,
                advance=sim.advance,
            )
        self.router = AccountRouter(runtimes)
        self._last_tick: pd.Timestamp | None = None

    def _touch(self, symbol: str):
        q = self.collector.feed.book.quotes.get(symbol)
        return (q.bid, q.ask) if q else (None, None)

    # ---- hooks the engine and the stream call -----------------------------------------------
    def attach(self, harness) -> None:
        harness.on_candidate.append(self._candidate)
        harness.on_reference_exit.append(self._reference_exit)

    def _candidate(self, rec: dict) -> None:
        try:
            self.router.route(pd.Timestamp(rec["signal_available_at"]), rec)
        except Exception as exc:  # noqa: BLE001 - one account's failure never stops the others
            self.errors.append(f"candidate {rec['candidate_id']}: {type(exc).__name__}: {exc}")
            log.exception("shadow account failed on a candidate")

    def _reference_exit(self, strategy_id: str, candidate_id: str, reason: str, when) -> None:
        try:
            self.router.on_reference_exit(when, f"acct:{strategy_id}", candidate_id, reason)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"exit {candidate_id}: {type(exc).__name__}: {exc}")
            log.exception("shadow account failed on a reference exit")

    def tick(self, now: pd.Timestamp) -> None:
        """The position loop, at most once per `tick_every` seconds."""
        if self._last_tick is not None and now - self._last_tick < self.tick_every:
            return
        self._last_tick = now
        for rt in self.router.runtimes.values():
            try:
                rt.on_tick(now)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"tick {rt.binding}: {type(exc).__name__}: {exc}")
                log.exception("shadow account tick failed")

    # ---- the end-of-session record ------------------------------------------------------------
    def summary(self) -> dict:
        out = {"errors": self.errors[:20], "accounts": {}}
        for binding, rt in self.router.runtimes.items():
            reasons: dict[str, int] = {}
            for d in rt.decisions:
                for r in d.reasons or ("ACCEPTED",):
                    reasons[r] = reasons.get(r, 0) + 1
            out["accounts"][binding] = {
                "decisions": len(rt.decisions),
                "outcomes": reasons,
                "orders": len(self.store.all_for(binding)),
                "open_contracts": self.store.filled_by_symbol(binding),
                "realized_pnl_usd": round(rt.realized_pnl(), 2),
                "halted": rt.halted,
                "cash_usd": round(self.brokers[binding].cash(), 2),
            }
        return out


def shadow_db_path(out: Path) -> Path:
    return out / "accounts.sqlite"
