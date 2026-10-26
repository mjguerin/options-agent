"""One strategy's account: candidate to order, and the position loop (FR-01, FR-02, EX-01).

A candidate passes these steps in order, and every refusal is recorded with its reasons:
1. it belongs to this account (wrong binding fails closed, FR-02, AC-01) and is not late,
   expired, or blocked by the daily limit, the order manager or the option feed;
2. contract selection (CS-01 to CS-03), then quantity from the premium budget (CS-05);
3. atomic reservation against the account limits (RK-03);
4. a final check of the quote just before sending (CS-06);
5. the order manager places the entry.

The position loop runs independently of strategy evaluation (EX-01): the premium stop, the
session cutoff, the daily loss limit, and the strategy's reference exits (D-33: execution
follows the reference model and never writes back to it). Every exit goes through the order
manager, which prevents duplicate liquidations (EX-05).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

import pandas as pd

from tournament.accounts.reservations import ReservationLedger
from tournament.live.quotes import QuoteView
from tournament.orders.manager import EntriesBlocked, OrderManager
from tournament.orders.model import Intent, Purpose, Status
from tournament.orders.store import IntentStore
from tournament.risk.config import RiskConfig
from tournament.risk.daily import DailyLossGuard, OpenPosition, premium_stop_hit
from tournament.risk.selection import Selection, select_contract
from tournament.risk.sizing import SizeResult, equity_basis, size_order


class WrongAccount(RuntimeError):
    """A candidate reached an account it does not belong to. Never routed elsewhere (FR-02)."""


class QuoteSource(Protocol):
    def view(self, symbol: str, now: pd.Timestamp) -> QuoteView: ...
    def entries_allowed(self, now: pd.Timestamp) -> bool: ...


class AccountView(Protocol):
    def cash(self) -> float: ...


@dataclass(frozen=True)
class EntryDecision:
    candidate_id: str
    accepted: bool
    reasons: tuple[str, ...]
    selection: Selection | None = None
    size: SizeResult | None = None
    intent: Intent | None = None
    at: pd.Timestamp | None = None


@dataclass
class AccountRuntime:
    binding: str
    strategy_id: str
    risk: RiskConfig
    orders: OrderManager
    store: IntentStore
    ledger: ReservationLedger
    quotes: QuoteSource
    account: AccountView
    contracts: pd.DataFrame  # symbol, expiration, strike, right
    today: date
    initial_equity: float
    start_of_day_equity: float
    cutoff_at: pd.Timestamp  # liquidation starts (SD-10)
    advance: Callable[[pd.Timestamp], None] = lambda now: None  # the modeled broker's clock
    decisions: list[EntryDecision] = field(default_factory=list)
    exits_requested: list[tuple[pd.Timestamp, str, str]] = field(default_factory=list)
    halted: str | None = None
    operator_halt: str | None = (
        None  # set by an operator control; cleared only by a validated resume
    )

    def __post_init__(self) -> None:
        self.guard = DailyLossGuard(self.risk.daily_loss, self.risk.sizing.contract_multiplier)

    # ---- entries ------------------------------------------------------------------------
    def on_candidate(self, now: pd.Timestamp, cand: dict) -> EntryDecision:
        if cand["account_binding"] != self.binding:
            raise WrongAccount(f"{cand['account_binding']!r} sent to {self.binding!r}")
        d = replace(self._decide(now, cand), at=now)
        self.decisions.append(d)
        return d

    def _reject(self, cid: str, *reasons: str, **kw) -> EntryDecision:
        return EntryDecision(cid, False, tuple(reasons), **kw)

    def _decide(self, now: pd.Timestamp, cand: dict) -> EntryDecision:
        cid = cand["candidate_id"]
        if not cand["submission_eligible"]:
            return self._reject(cid, "LATE_SIGNAL")  # D-37
        if now >= pd.Timestamp(cand["expires_at"]):
            return self._reject(cid, "CANDIDATE_EXPIRED")
        if self.halted:
            return self._reject(cid, self.halted)
        if self.operator_halt:
            return self._reject(cid, self.operator_halt)
        if self.orders.entries_blocked:
            return self._reject(cid, "ENTRIES_BLOCKED")
        if not self.quotes.entries_allowed(now):
            return self._reject(cid, "OPTION_FEED_DOWN")  # AC-08

        sel = select_contract(
            cand["underlying_direction"],
            float(cand["entry_reference"]),
            self.today,
            self.contracts,
            lambda s: self.quotes.view(s, now),
            self.risk.contract_selection,
        )
        if not sel.selected:
            return self._reject(cid, *sel.reasons, selection=sel)  # CS-03

        basis = equity_basis(self.risk, self.start_of_day_equity, self.initial_equity)
        q = sel.quote
        size = size_order(q.ask, q.ask_size, basis, self.account.cash(), self.risk)
        if not size.sized:
            return self._reject(cid, *size.reasons, selection=sel, size=size)

        got = self.ledger.reserve(
            now, self.binding, cid, size.premium_at_risk, size.quantity, basis
        )
        if not got.ok:
            return self._reject(cid, *got.reasons, selection=sel, size=size)

        # CS-06: the price may have moved since selection; look again before sending
        fresh = self.quotes.view(sel.symbol, now)
        if not fresh.usable or fresh.quote.ask > q.ask + 1e-9:
            self.ledger.release(now, self.binding, cid, "QUOTE_MOVED")
            return self._reject(cid, "QUOTE_MOVED", *fresh.reasons, selection=sel, size=size)
        try:
            intent = self.orders.place_entry(
                now,
                cid,
                self.strategy_id,
                sel.symbol,
                size.quantity,
                fresh.quote.bid,
                fresh.quote.ask,
            )
        except EntriesBlocked:
            self.ledger.release(now, self.binding, cid, "ENTRIES_BLOCKED")
            return self._reject(cid, "ENTRIES_BLOCKED", selection=sel, size=size)
        if intent.status is Status.REJECTED:
            self.ledger.release(now, self.binding, cid, "BROKER_REJECTED")
        return EntryDecision(cid, True, (), sel, size, intent)

    def restore_state(self) -> dict:
        """After a restart, bring back what must survive a crash: the daily-limit halt, an
        operator pause or flatten, and an entry block. They are read back from the account's own
        event log, in order, so the last word wins (a resume after a pause clears it)."""
        restored: dict = {}
        for kind, detail in self.store.events(self.binding):
            if kind == "operator_pause":
                self.operator_halt = "OPERATOR_PAUSE"
            elif kind == "operator_flatten":
                self.operator_halt = detail.get("reason", "OPERATOR_FLATTEN")
            elif kind == "operator_resume":
                self.operator_halt = None
            elif kind == "daily_limit":
                self.halted = "DAILY_LIMIT"
            elif kind == "entries_blocked":
                self.orders.entries_blocked = f"restored after restart: {detail.get('diffs', '')}"
            elif kind == "entries_unblocked":
                self.orders.entries_blocked = None
        if self.operator_halt:
            restored["operator_halt"] = self.operator_halt
        if self.halted:
            restored["halted"] = self.halted
        if self.orders.entries_blocked:
            restored["entries_blocked"] = self.orders.entries_blocked
        return restored

    # ---- operator controls (BRD 14; RK-02) ---------------------------------------------------
    # Pausing entries, cancelling pending orders and closing positions are different things and
    # are different methods. None of them changes strategy state (D-33).
    def operator_pause(self, now: pd.Timestamp) -> dict:
        self.operator_halt = "OPERATOR_PAUSE"
        self.store.event(self.binding, now, "operator_pause")
        return {"entries": "paused"}

    def operator_resume(self, now: pd.Timestamp) -> tuple[bool, list[str]]:
        """Resume only after validation: the day's loss limit stands, and nothing unresolved."""
        why = []
        if self.halted == "DAILY_LIMIT":
            why.append("DAILY_LIMIT")
        if self.orders.entries_blocked:
            why.append("ENTRIES_BLOCKED")
        if not self.quotes.entries_allowed(now):
            why.append("OPTION_FEED_DOWN")
        unresolved = {Status.UNCERTAIN, Status.NOT_FOUND, Status.CANCEL_REQUESTED, Status.SUBMITTED}
        if any(i.status in unresolved for i in self.store.active(self.binding)):
            why.append("ORDERS_UNRESOLVED")
        if why:
            return False, why
        self.operator_halt = None
        self.store.event(self.binding, now, "operator_resume")
        return True, []

    def cancel_pending(self, now: pd.Timestamp, reason: str = "OPERATOR_CANCEL") -> int:
        """Cancel entry orders that have not filled. Held positions are not touched."""
        n = 0
        for i in self.store.active(self.binding):
            if i.purpose is Purpose.ENTRY:
                self.orders.abandon_entry(now, i.candidate_id, reason)
                n += 1
        return n

    def close_position(self, now: pd.Timestamp, symbol: str) -> bool:
        if self.orders.position(symbol) <= 0:
            return False
        self._exit(now, self._candidate_of(symbol), symbol, "OPERATOR_CLOSE")
        return True

    def flatten(self, now: pd.Timestamp, reason: str = "OPERATOR_FLATTEN") -> dict:
        cancelled = self.cancel_pending(now, reason)
        closed = []
        for symbol, qty in self.store.filled_by_symbol(self.binding).items():
            if qty > 0:
                self._exit(now, self._candidate_of(symbol), symbol, reason)
                closed.append(symbol)
        self.operator_halt = reason
        self.store.event(
            self.binding, now, "operator_flatten", reason=reason, cancelled=cancelled, closed=closed
        )
        return {"cancelled_entries": cancelled, "exits_placed": closed}

    # ---- the strategy's reference exit (D-33, D-34) -----------------------------------------
    def on_reference_exit(self, now: pd.Timestamp, candidate_id: str, reason: str) -> None:
        self.orders.abandon_entry(now, candidate_id, f"REF_{reason}")
        for symbol in list(self.store.filled_by_symbol(self.binding)):
            self._exit(now, candidate_id, symbol, f"REF_{reason}")

    def _exit(self, now: pd.Timestamp, candidate_id: str, symbol: str, reason: str) -> None:
        v = self.quotes.view(symbol, now)
        # only a usable quote gives a price; otherwise the order manager follows the exit
        # contingency (market for a forced exit, wait and alert for a discretionary one)
        bid = v.quote.bid if (v.usable and v.quote is not None) else None
        ask = v.quote.ask if (v.usable and v.quote is not None) else None
        x = self.orders.place_exit(now, candidate_id, self.strategy_id, symbol, reason, bid, ask)
        if x is not None:
            self.exits_requested.append((now, symbol, reason))

    # ---- the position loop (EX-01) -----------------------------------------------------------
    def on_tick(self, now: pd.Timestamp) -> None:
        self.advance(now)
        self.orders.poll(now)
        self.ledger.refresh(now, self.binding, self.store)
        positions = self._open_positions(now)
        for p in positions:
            cand = self._candidate_of(p.symbol)
            triggered = []
            if now >= self.cutoff_at:
                triggered.append("SESSION_CUTOFF")
            if premium_stop_hit(p.entry_price, p.bid, self.risk.premium_stop.loss_pct):
                triggered.append("PREMIUM_STOP")
            for reason in triggered:  # one order; later triggers add their reason (EX-05, EX-07)
                self._exit(now, cand, p.symbol, reason)
        status = self.guard.update(now, self.realized_pnl(), positions)
        if status.breached and self.halted is None:
            self.halted = "DAILY_LIMIT"
            self.orders.store.event(self.binding, now, "daily_limit", loss=status.loss)
        if status.flatten:
            for p in positions:
                self._exit(now, self._candidate_of(p.symbol), p.symbol, "DAILY_LOSS_FLATTEN")

    # ---- accounting ---------------------------------------------------------------------
    def _candidate_of(self, symbol: str) -> str:
        entries = [
            i
            for i in self.store.all_for(self.binding)
            if i.symbol == symbol and i.purpose is Purpose.ENTRY and i.qty_filled > 0
        ]
        return entries[-1].candidate_id if entries else symbol

    def _avg_entry(self, symbol: str) -> float:
        buys = [
            i
            for i in self.store.all_for(self.binding)
            if i.symbol == symbol and i.side == "buy" and i.qty_filled
        ]
        n = sum(i.qty_filled for i in buys)
        return sum(i.qty_filled * (i.avg_fill_price or 0.0) for i in buys) / n if n else 0.0

    def _open_positions(self, now: pd.Timestamp) -> list[OpenPosition]:
        out = []
        for symbol, qty in self.store.filled_by_symbol(self.binding).items():
            if qty <= 0:
                continue
            v = self.quotes.view(symbol, now)
            bid = v.quote.bid if (v.usable and v.quote is not None) else None
            out.append(OpenPosition(symbol, qty, self._avg_entry(symbol), bid))
        return out

    def positions_detail(self, now: pd.Timestamp) -> list[dict]:
        """Held positions with the latest usable bid and the open profit or loss at it."""
        m = self.risk.sizing.contract_multiplier
        return [
            {
                "symbol": p.symbol,
                "qty": p.quantity,
                "avg_entry": round(p.entry_price, 4),
                "bid": p.bid,
                "unrealized_usd": None
                if p.bid is None
                else round((p.bid - p.entry_price) * p.quantity * m, 2),
            }
            for p in self._open_positions(now)
        ]

    def realized_pnl(self) -> float:
        """Profit and loss on closed contracts, at average cost, from the orders' fills."""
        m = self.risk.sizing.contract_multiplier
        cost: dict[str, tuple[int, float]] = {}  # symbol -> (bought qty, total cost)
        sold: dict[str, tuple[int, float]] = {}
        for i in self.store.all_for(self.binding):
            if not i.qty_filled:
                continue
            book = cost if i.side == "buy" else sold
            q, t = book.get(i.symbol, (0, 0.0))
            book[i.symbol] = (q + i.qty_filled, t + i.qty_filled * (i.avg_fill_price or 0.0))
        pnl = 0.0
        for symbol, (sq, proceeds) in sold.items():
            bq, spent = cost.get(symbol, (0, 0.0))
            avg = spent / bq if bq else 0.0
            pnl += proceeds - sq * avg
        return pnl * m


@dataclass
class AccountRouter:
    """Candidates go to the account bound to their strategy, and nowhere else (FR-02)."""

    runtimes: dict[str, AccountRuntime]

    def route(self, now: pd.Timestamp, cand: dict) -> EntryDecision:
        rt = self.runtimes.get(cand["account_binding"])
        if rt is None:
            raise WrongAccount(f"no account bound to {cand['account_binding']!r}")
        return rt.on_candidate(now, cand)

    def on_reference_exit(self, now: pd.Timestamp, binding: str, cid: str, reason: str) -> None:
        rt = self.runtimes.get(binding)
        if rt is None:
            raise WrongAccount(f"no account bound to {binding!r}")
        rt.on_reference_exit(now, cid, reason)

    def on_tick(self, now: pd.Timestamp) -> None:
        for rt in self.runtimes.values():
            rt.on_tick(now)
