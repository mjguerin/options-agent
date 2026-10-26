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
from tournament.live.quotes import usable_touch
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
        reconcile_every_s: float,
    ) -> None:
        self.risk, self.orders_cfg = load_risk_config(), load_orders_config()
        self.collector, self.tick_every = collector, pd.Timedelta(seconds=tick_every_s)
        self.reconcile_every = pd.Timedelta(seconds=reconcile_every_s)
        self._last_reconcile: pd.Timestamp | None = None
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
            sim = SimBroker(collector.feed, self.orders_cfg.simulation, starting_balance, mult)
            self.brokers[binding] = sim
            runtimes[binding] = AccountRuntime(
                binding,
                sid,
                self.risk,
                OrderManager(
                    binding,
                    sim,
                    self.store,
                    self.orders_cfg,
                    quotes=lambda s, now: usable_touch(collector.feed.view(s, now)),
                ),
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
        # a restart picks up the day's books: the modeled broker's orders and cash, and the
        # halts and blocks that must survive a crash (D-67)
        self.restored: dict[str, dict] = {}
        for binding, rt in runtimes.items():
            n = self.brokers[binding].restore(self.store, binding)
            state = rt.restore_state()
            if n or state:
                self.restored[binding] = {"orders": n, **state}

    # ---- hooks the engine and the stream call -----------------------------------------------
    def attach(self, harness) -> None:
        harness.on_candidate.append(self._candidate)
        harness.on_reference_exit.append(self._reference_exit)

    def held_symbols(self) -> set[str]:
        """Contracts whose quotes must not be dropped: held positions and working orders."""
        out: set[str] = set()
        for binding in self.router.runtimes:
            out |= set(self.store.filled_by_symbol(binding))
            out |= {i.symbol for i in self.store.active(binding)}
        return out

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

    def recover(self, now: pd.Timestamp, closed_references: list[tuple[str, str, str]]) -> None:
        """After a restart, settle the execution side against the rebuilt strategy side, before
        any new entry. `closed_references` are (strategy, candidate, reason) for every reference
        position the strategy layer closed so far today: an order that outlived its reference
        position is cancelled, and a position that outlived it is sold (D-33, D-34). A failure
        halts that account and says so (never a silent flat start)."""
        for binding, rt in self.router.runtimes.items():
            try:
                rt.orders.recover(now)
                rt.orders.reconcile_positions(now)
                rt.ledger.refresh(now, binding, self.store)
                for sid, cid, reason in closed_references:
                    if f"acct:{sid}" == binding:
                        rt.on_reference_exit(now, cid, reason)
            except Exception as exc:  # noqa: BLE001
                rt.operator_halt = "RECOVERY_FAILED"
                self.errors.append(f"recovery {binding}: {type(exc).__name__}: {exc}")
                log.exception("account recovery failed")

    def post_close(self, now: pd.Timestamp) -> dict:
        """The after-close reconciliation (BRD 10.6): cancel what is still pending, compare our
        books with the modeled broker's, and list what is unresolved. Never says flat because
        local state says so: the broker's positions are asked."""
        report: dict[str, dict] = {}
        for binding, rt in self.router.runtimes.items():
            try:
                cancelled = rt.cancel_pending(now, "POST_CLOSE")
                rt.orders.poll(now)
                diffs = rt.orders.reconcile_positions(now)
                stuck = rt.orders.unresolved(now, 0.0)
                held = rt.store.filled_by_symbol(binding)
                report[binding] = {
                    "cancelled_entries": cancelled,
                    "position_mismatches": [list(d) for d in diffs],
                    "unresolved_orders": [i.client_order_id for i in stuck],
                    "still_held": held,
                    "flat": not held and not diffs and not stuck,
                }
            except Exception as exc:  # noqa: BLE001
                report[binding] = {"error": f"{type(exc).__name__}: {exc}", "flat": False}
        return report

    def tick(self, now: pd.Timestamp) -> None:
        """The position loop, at most once per `tick_every` seconds."""
        if self._last_tick is not None and now - self._last_tick < self.tick_every:
            return
        self._last_tick = now
        due = self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_every
        if due:
            self._last_reconcile = now
        for rt in self.router.runtimes.values():
            try:
                rt.on_tick(now)
                if due:  # a difference beyond the tolerance blocks entries until cleared (OM-06)
                    rt.orders.reconcile_positions(now)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"tick {rt.binding}: {type(exc).__name__}: {exc}")
                log.exception("shadow account tick failed")

    # ---- the end-of-session record ------------------------------------------------------------
    def summary(self) -> dict:
        out = {"errors": self.errors[:20], "accounts": {}, "restored": self.restored}
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


def has_exposure(db_path: Path) -> bool:
    """Does the day's account database hold a position or an order still in flight? Read-only,
    so asking never changes it."""
    if not db_path.exists():
        return False
    engine = create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true")
    try:
        store = IntentStore(engine)
        from sqlalchemy import select

        from tournament.orders.model import ACTIVE
        from tournament.orders.store import orders

        with engine.connect() as c:
            accounts = {r.account_binding for r in c.execute(select(orders.c.account_binding))}
            active = c.execute(
                select(orders.c.client_order_id).where(
                    orders.c.status.in_([s.value for s in ACTIVE])
                )
            ).first()
        return active is not None or any(store.filled_by_symbol(a) for a in accounts)
    finally:
        engine.dispose()


def shadow_db_path(day_dir: Path, fresh: bool = False) -> Path:
    """One durable accounts database per session day, shared by every engine start of that day,
    so a restart picks up the same books. A deliberate fresh simulation gets its own."""
    return day_dir / ("accounts-fresh.sqlite" if fresh else "accounts.sqlite")
