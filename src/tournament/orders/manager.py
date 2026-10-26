"""The order manager (OM-01 to OM-08, EX-03 to EX-05; D-59 to D-62).

Everything that reaches the broker passes through here, for one account. The rules:
- Persist the intent and its client order ID before submitting (OM-02).
- After an uncertain submission, look the order up by client order ID, never assume it failed
  (OM-03). Only a confirmed absence allows a resubmission, and it reuses the same ID, so a late
  original can't double up.
- Quantities intended, submitted and filled are kept apart (OM-07). Fills only grow.
- One entry per candidate, however often asked (OM-08). One exit chain per position: a second
  trigger adds its reason to the working order instead of selling again (EX-05).
- Timeouts and re-pricing follow the order config. A cancel is confirmed by lookup, because a
  fill can beat it.
- Exposure the store doesn't explain blocks new entries (OM-06).

Time is passed in; waiting between lookups uses an injectable sleep, so tests run instantly.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pandas as pd

from tournament.orders.broker import (
    Broker,
    BrokerOrder,
    BrokerReject,
    BrokerUncertain,
    OrderRequest,
)
from tournament.orders.config import OrdersConfig
from tournament.orders.model import Intent, Purpose, Status
from tournament.orders.store import IntentStore


class EntriesBlocked(RuntimeError):
    pass


def _round(price: float, tick: float, mode: str) -> float:
    t = Decimal(str(tick))
    steps = Decimal(str(price)) / t
    n = steps.to_integral_value(rounding=ROUND_CEILING if mode == "up" else ROUND_FLOOR)
    return float(max(n, Decimal(1)) * t)


class OrderManager:
    def __init__(
        self,
        account: str,
        broker: Broker,
        store: IntentStore,
        cfg: OrdersConfig,
        sleep: Callable[[float], None] = time.sleep,
        quotes: Callable[[str, pd.Timestamp], tuple[float | None, float | None]] | None = None,
        quote_detail: Callable[[str, pd.Timestamp], dict] | None = None,
    ) -> None:
        self.account, self.broker, self.store, self.cfg, self.sleep = (
            account,
            broker,
            store,
            cfg,
            sleep,
        )
        self.entries_blocked: str | None = None
        # a usable (bid, ask) for a contract at a time, or (None, None); how exits are re-priced
        self.quotes = quotes or (lambda symbol, now: (None, None))
        # the provenance of that quote (times, sizes, quality), recorded with the decision (D-80)
        self.quote_detail = quote_detail or (lambda symbol, now: {})
        # exits waiting for a usable bid: symbol -> {since, candidate_id, strategy_id, reasons}
        self.blind: dict[str, dict] = {}

    # ---- ids and prices -----------------------------------------------------------------
    def client_order_id(self, candidate_id: str, purpose: Purpose, n: int) -> str:
        acct = re.sub(r"[^A-Za-z0-9]", "", self.account)
        h = hashlib.sha256(candidate_id.encode()).hexdigest()[:10]
        letter = {Purpose.ENTRY: "e", Purpose.EXIT: "x"}[purpose]
        return f"{self.cfg.client_order_id.prefix}-{acct}-{h}-{letter}{n}"

    def entry_price(self, bid: float, ask: float) -> float:
        e, tick = self.cfg.entry, self.cfg.pricing.tick_size
        basis = ask if e.price_basis == "ask" else (bid + ask) / 2
        return _round(basis + e.price_offset_ticks * tick, tick, "up")

    def exit_price(self, bid: float, ask: float | None) -> float:
        """The first exit limit, from a usable bid. With no usable bid there is no price to
        compute: the caller follows the exit contingency instead (never a fabricated price)."""
        x, tick = self.cfg.exit, self.cfg.pricing.tick_size
        basis = bid if x.price_basis == "bid" else (bid + (ask if ask else bid)) / 2
        return _round(basis + x.price_offset_ticks * tick, tick, "down")

    def forced(self, reasons) -> bool:
        return any(r in self.cfg.exit.forced_reasons for r in reasons)

    def _blind_action(self, reasons) -> str:
        x = self.cfg.exit
        return x.no_usable_bid_forced if self.forced(reasons) else x.no_usable_bid_discretionary

    # ---- entries ------------------------------------------------------------------------
    def place_entry(
        self,
        now: pd.Timestamp,
        candidate_id: str,
        strategy_id: str,
        symbol: str,
        qty: int,
        bid: float,
        ask: float,
    ) -> Intent:
        """One entry per candidate: asking again returns the existing order (OM-08)."""
        chain = self.store.chain(self.account, candidate_id, Purpose.ENTRY)
        if chain:
            return chain[-1]
        if self.entries_blocked:
            raise EntriesBlocked(self.entries_blocked)
        e = self.cfg.entry
        intent = Intent(
            self.client_order_id(candidate_id, Purpose.ENTRY, 0),
            self.account,
            candidate_id,
            strategy_id,
            Purpose.ENTRY,
            "buy",
            symbol,
            qty,
            e.order_type,
            self.entry_price(bid, ask),
            e.time_in_force,
            Status.PENDING_SUBMIT,
            now,
        )
        self.store.insert(intent)  # before anything is sent (OM-02)
        # the quote the decision was made on: the evaluation ledger prices the trade from it
        self.store.event(
            intent.client_order_id,
            now,
            "intent",
            qty=qty,
            limit=intent.limit_price,
            bid=bid,
            ask=ask,
            decision_at=now.isoformat(),
            quote=self.quote_detail(symbol, now),
        )
        return self._send(intent, now)

    def abandon_entry(self, now: pd.Timestamp, candidate_id: str, reason: str) -> list[Intent]:
        """The strategy's reference position has already exited (D-34): an unfilled entry is
        cancelled and never re-priced. A fill that beats the cancel stays and is exited by the
        caller."""
        done = []
        for i in self.store.chain(self.account, candidate_id, Purpose.ENTRY):
            if not i.active:
                continue
            self.store.event(i.client_order_id, now, "abandon", reason=reason)
            if i.status in (Status.PENDING_SUBMIT, Status.SUBMITTED, Status.UNCERTAIN):
                i = self._reconcile(i, now)  # find out what exists before cancelling it
            if i.active:
                i = self._cancel(i, now, replace=False)
            done.append(i)
        return done

    # ---- exits --------------------------------------------------------------------------
    def position(self, symbol: str) -> int:
        return self.store.filled_by_symbol(self.account).get(symbol, 0)

    def place_exit(
        self,
        now: pd.Timestamp,
        candidate_id: str,
        strategy_id: str,
        symbol: str,
        reason: str,
        bid: float | None,
        ask: float | None = None,
    ) -> Intent | None:
        """Sell what is held and not already being sold (EX-04). A second trigger while an exit
        works only adds its reason (EX-05, EX-07). Returns None when there is nothing to sell."""
        active = [
            i
            for i in self.store.active(self.account)
            if i.symbol == symbol and i.purpose is Purpose.EXIT
        ]
        if active:
            cur = active[-1]
            if reason not in cur.reasons:
                cur = cur.with_(reasons=(*cur.reasons, reason))
                self.store.save(cur)
                self.store.event(cur.client_order_id, now, "exit_reason_added", reason=reason)
            return cur
        qty = self.position(symbol)
        if qty <= 0:
            self.blind.pop(symbol, None)
            return None
        usable = bid is not None and bid > 0
        reasons = (*self.blind.get(symbol, {}).get("reasons", ()), reason)
        if not usable and self._blind_action(reasons) == "hold":
            b = self.blind.setdefault(
                symbol,
                {
                    "since": now,
                    "candidate_id": candidate_id,
                    "strategy_id": strategy_id,
                    "reasons": [],
                },
            )
            if reason not in b["reasons"]:
                b["reasons"].append(reason)
                self.store.event(self.account, now, "exit_blind", symbol=symbol, reason=reason)
            return None
        self.blind.pop(symbol, None)
        chain = self.store.chain(self.account, candidate_id, Purpose.EXIT)
        n = len(chain)  # a new attempt after an exhausted chain keeps IDs unique
        x = self.cfg.exit
        order_type, price = (
            (x.order_type, self.exit_price(bid, ask)) if usable else ("market", None)
        )
        intent = Intent(
            self.client_order_id(candidate_id, Purpose.EXIT, n),
            self.account,
            candidate_id,
            strategy_id,
            Purpose.EXIT,
            "sell",
            symbol,
            qty,
            order_type,
            price,
            x.time_in_force,
            Status.PENDING_SUBMIT,
            now,
            replacement=n,
            reasons=tuple(dict.fromkeys(reasons)),
        )
        self.store.insert(intent)
        self.store.event(
            intent.client_order_id,
            now,
            "intent",
            qty=qty,
            reason=reason,
            order_type=order_type,
            bid=bid if usable else None,
            ask=ask if usable else None,
            decision_at=now.isoformat(),
            quote=self.quote_detail(symbol, now) if usable else {},
        )
        return self._send(intent, now)

    # ---- sending and reconciling ----------------------------------------------------------
    def _send(self, intent: Intent, now: pd.Timestamp, resubmitted: bool = False) -> Intent:
        intent = intent.with_(
            status=Status.SUBMITTED, qty_submitted=intent.qty_intended, submitted_at=now
        )
        self.store.save(intent)
        self.store.event(intent.client_order_id, now, "submit", resubmitted=resubmitted)
        req = OrderRequest(
            intent.client_order_id,
            intent.symbol,
            intent.side,
            intent.qty_intended,
            intent.order_type,
            intent.limit_price,
            intent.time_in_force,
        )
        try:
            bo = self.broker.submit(req)
        except BrokerReject as exc:
            intent = intent.with_(status=Status.REJECTED, last_error=str(exc))
            self.store.save(intent)
            self.store.event(intent.client_order_id, now, "rejected", error=str(exc))
            return intent
        except BrokerUncertain as exc:
            intent = intent.with_(status=Status.UNCERTAIN, last_error=str(exc))
            self.store.save(intent)
            self.store.event(intent.client_order_id, now, "uncertain", error=str(exc))
            return intent if resubmitted else self._reconcile(intent, now)
        return self._apply(intent, bo, now)

    def _apply(self, intent: Intent, bo: BrokerOrder, now: pd.Timestamp) -> Intent:
        status = bo.status
        if intent.status is Status.CANCEL_REQUESTED and status in (
            Status.OPEN,
            Status.PARTIALLY_FILLED,
        ):
            status = Status.CANCEL_REQUESTED  # still waiting for the broker to confirm
        new = intent.with_(
            status=status,
            broker_order_id=bo.broker_order_id,
            qty_filled=max(intent.qty_filled, bo.filled_qty),  # fills only grow (OM-07)
            avg_fill_price=bo.filled_avg_price or intent.avg_fill_price,
        )
        if new != intent:
            self.store.save(new)
            if new.status != intent.status or new.qty_filled != intent.qty_filled:
                self.store.event(
                    new.client_order_id,
                    now,
                    "status",
                    status=new.status.value,
                    filled=new.qty_filled,
                )
        return new

    def _lookup(self, intent: Intent) -> BrokerOrder | None | str:
        try:
            return self.broker.get_by_client_id(intent.client_order_id)
        except BrokerUncertain:
            return "unreachable"

    def _reconcile(self, intent: Intent, now: pd.Timestamp) -> Intent:
        """OM-03: find out what happened to a submission before doing anything else."""
        s = self.cfg.submission
        for attempt in range(s.reconcile_attempts):
            found = self._lookup(intent)
            if isinstance(found, BrokerOrder):
                self.store.event(intent.client_order_id, now, "reconciled", found=True)
                return self._apply(intent, found, now)
            if attempt + 1 < s.reconcile_attempts:
                self.sleep(s.reconcile_interval_s)
        if found == "unreachable":  # the broker couldn't be asked: absence is not established
            return intent
        intent = intent.with_(status=Status.NOT_FOUND)
        self.store.save(intent)
        self.store.event(intent.client_order_id, now, "not_found")
        if s.resubmit_after_not_found:
            return self._send(intent, now, resubmitted=True)
        return intent

    def recover(self, now: pd.Timestamp) -> list[Intent]:
        """After a restart (AC-09): settle every order that was in flight before new work."""
        out = []
        for i in self.store.active(self.account):
            found = self._lookup(i)
            if isinstance(found, BrokerOrder):
                out.append(self._apply(i, found, now))
            elif found == "unreachable":
                out.append(i)
            elif i.status is Status.PENDING_SUBMIT:  # crashed before sending
                out.append(self._send(i, now))
            else:
                out.append(self._reconcile(i, now))
        return out

    # ---- polling, timeouts, replacements --------------------------------------------------
    def poll(self, now: pd.Timestamp) -> list[Intent]:
        for symbol, b in list(self.blind.items()):  # exits waiting for a usable bid
            bid, ask = self.quotes(symbol, now)
            if bid is not None and bid > 0:
                for reason in list(b["reasons"]):
                    self.place_exit(
                        now, b["candidate_id"], b["strategy_id"], symbol, reason, bid, ask
                    )
        out = []
        for i in self.store.active(self.account):
            if i.status in (Status.SUBMITTED, Status.UNCERTAIN):
                i = self._reconcile(i, now)
            elif i.status is not Status.PENDING_SUBMIT:
                found = self._lookup(i)
                if isinstance(found, BrokerOrder):
                    i = self._apply(i, found, now)
            if i.active and i.status is not Status.PENDING_SUBMIT:
                i = self._timeouts(i, now)
            out.append(i)
        return out

    def _timeout_s(self, i: Intent) -> float:
        return (self.cfg.entry if i.purpose is Purpose.ENTRY else self.cfg.exit).fill_timeout_s

    def _timeouts(self, i: Intent, now: pd.Timestamp) -> Intent:
        waiting = i.status in (Status.OPEN, Status.PARTIALLY_FILLED, Status.CANCEL_REQUESTED)
        if waiting and i.submitted_at is not None:
            if (now - i.submitted_at).total_seconds() >= self._timeout_s(i):
                return self._cancel(i, now)
        return i

    def _cancel(self, i: Intent, now: pd.Timestamp, replace: bool = True) -> Intent:
        if i.broker_order_id:
            try:
                self.broker.cancel(i.broker_order_id)
            except BrokerUncertain:
                self.store.event(i.client_order_id, now, "cancel_uncertain")
        if i.status is not Status.CANCEL_REQUESTED:
            i = i.with_(status=Status.CANCEL_REQUESTED)
            self.store.save(i)
            self.store.event(i.client_order_id, now, "cancel_requested")
        s = self.cfg.submission
        for attempt in range(s.cancel_confirm_attempts):
            found = self._lookup(i)
            if isinstance(found, BrokerOrder):
                i = self._apply(i, found, now)
            if not i.active:
                return self._after_cancel(i, now) if replace else i
            if attempt + 1 < s.cancel_confirm_attempts:
                self.sleep(s.cancel_confirm_interval_s)
        return i  # unresolved: the next poll asks again

    def _after_cancel(self, i: Intent, now: pd.Timestamp) -> Intent:
        """What follows a finished (cancelled or expired) order: stop, re-price or fall back."""
        if i.remaining <= 0 or i.status in (Status.FILLED, Status.REJECTED):
            return i
        tick = self.cfg.pricing.tick_size
        chain = self.store.chain(self.account, i.candidate_id, i.purpose)
        first = chain[0].limit_price if chain and chain[0].limit_price else i.limit_price
        n = i.replacement + 1
        if i.purpose is Purpose.ENTRY:
            e = self.cfg.entry
            # a partly filled entry is kept as it is; only an untouched one is re-priced
            if i.qty_filled > 0 or i.replacement >= e.max_replacements:
                return i
            steps = n * e.replacement_step_ticks
            if steps > e.max_slippage_ticks:
                self.store.event(i.client_order_id, now, "replacement_capped")
                return i
            order_type, price = e.order_type, round(first + steps * tick, 10)
        else:
            x = self.cfg.exit
            # sell only what is still held: the position, not the order's memory, is the truth
            held = self.position(i.symbol)
            if min(i.remaining, held) <= 0:
                self.store.event(i.client_order_id, now, "exit_nothing_left")
                return i
            if i.replacement < x.max_replacements:
                bid, _ = self.quotes(i.symbol, now)
                if bid is None or bid <= 0:
                    if self._blind_action(i.reasons) == "hold":
                        self.blind[i.symbol] = {
                            "since": now,
                            "candidate_id": i.candidate_id,
                            "strategy_id": i.strategy_id,
                            "reasons": list(i.reasons),
                        }
                        self.store.event(i.client_order_id, now, "exit_blind", symbol=i.symbol)
                        return i
                    order_type, price = "market", None
                else:
                    order_type = x.order_type
                    steps = n * x.replacement_step_ticks  # from a fresh bid, not the last limit
                    raw = bid + x.price_offset_ticks * tick - steps * tick
                    price = max(_round(raw, tick, "down"), tick)
            elif x.final_fallback == "market":
                order_type, price = "market", None
            else:
                self.store.event(i.client_order_id, now, "exit_unfilled", remaining=i.remaining)
                return i
        nxt = Intent(
            self.client_order_id(i.candidate_id, i.purpose, n),
            self.account,
            i.candidate_id,
            i.strategy_id,
            i.purpose,
            i.side,
            i.symbol,
            i.remaining
            if i.purpose is Purpose.ENTRY
            else min(i.remaining, self.position(i.symbol)),
            order_type,
            price,
            i.time_in_force,
            Status.PENDING_SUBMIT,
            now,
            replacement=n,
            parent_client_order_id=i.client_order_id,
            reasons=i.reasons,
        )
        self.store.insert(nxt)
        self.store.event(nxt.client_order_id, now, "intent", qty=nxt.qty_intended, limit=price)
        return self._send(nxt, now)

    def unresolved(self, now: pd.Timestamp, older_than_s: float) -> list[Intent]:
        """Orders whose fate we don't know. They block blind resubmission and raise an incident."""
        bad = {Status.UNCERTAIN, Status.SUBMITTED, Status.CANCEL_REQUESTED, Status.NOT_FOUND}
        out = []
        for i in self.store.all_for(self.account):
            started = i.submitted_at or i.created_at
            if i.status in bad and (now - started).total_seconds() >= older_than_s:
                out.append(i)
        return out

    # ---- exposure checks (OM-05, OM-06) ---------------------------------------------------
    def reconcile_positions(self, now: pd.Timestamp) -> list[tuple[str, int, int]]:
        """Compare our fills with the broker's positions. Differences beyond the tolerance are
        returned, and by config block new entries until someone clears them."""
        theirs = {p.symbol: p.qty for p in self.broker.positions()}
        ours = self.store.filled_by_symbol(self.account)
        tol = self.cfg.reconciliation.quantity_tolerance
        diffs = [
            (s, ours.get(s, 0), theirs.get(s, 0))
            for s in sorted(set(ours) | set(theirs))
            if abs(ours.get(s, 0) - theirs.get(s, 0)) > tol
        ]
        if diffs and self.cfg.reconciliation.block_entries_on_mismatch:
            self.entries_blocked = f"position mismatch: {diffs}"
            self.store.event(self.account, now, "entries_blocked", diffs=diffs)
        return diffs

    def clear_entries_block(self, now: pd.Timestamp, who: str) -> None:
        self.store.event(self.account, now, "entries_unblocked", by=who)
        self.entries_blocked = None
