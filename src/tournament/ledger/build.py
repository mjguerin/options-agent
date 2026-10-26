"""Building the two ledgers for one account from its orders (BRD 12.3; AC-11).

The broker ledger is what the broker side recorded: each order's filled quantity and average price,
the cash those fills moved, and the positions left. The evaluation ledger re-prices the same
trades under one declared set of assumptions: buy at the ask quoted when the entry was decided,
sell at the bid quoted when the exit was decided, a configured number of ticks worse, and the
configured per-contract costs. Every competitor is priced the same way, so a result reflects its
decisions and not a lucky or unlucky fill. The two are never netted into each other: both are
reported, with the difference, and a trade with no usable quote is flagged rather than priced from
thin air.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tournament.ledger.config import LedgerConfig
from tournament.live.subscriptions import parse_occ
from tournament.orders.model import Intent, Purpose
from tournament.orders.store import IntentStore

NO_ENTRY_QUOTE, NO_EXIT_QUOTE = "NO_ENTRY_QUOTE", "NO_EXIT_QUOTE"
OPEN_AT_END = "OPEN_AT_END"


@dataclass
class Fill:
    client_order_id: str
    candidate_id: str
    purpose: str
    side: str
    symbol: str
    qty: int
    price: float
    cash_usd: float  # negative when money left the account
    at: pd.Timestamp | None


@dataclass
class Trade:
    candidate_id: str
    strategy_id: str
    symbol: str
    right: str | None
    entry_at: pd.Timestamp | None
    exit_at: pd.Timestamp | None
    qty: int  # contracts bought
    closed_qty: int
    open_qty: int
    broker_entry: float
    broker_exit: float | None
    eval_entry: float
    eval_exit: float | None
    broker_gross_usd: float
    eval_gross_usd: float
    fees_usd: float
    net_eval_usd: float
    stress_net_usd: dict[str, float] = field(default_factory=dict)
    holding_s: float | None = None
    premium_usd: float = 0.0  # entry notional
    exit_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["entry_at"] = None if self.entry_at is None else self.entry_at.isoformat()
        d["exit_at"] = None if self.exit_at is None else self.exit_at.isoformat()
        return d


def _fill_time(store: IntentStore, order: Intent) -> pd.Timestamp | None:
    """When the order first showed a fill: the first status event with a filled quantity."""
    for at, kind, detail in store.event_rows(order.client_order_id):
        if kind == "status" and detail.get("filled", 0) > 0:
            return at
    return order.submitted_at


def _decision_quote(store: IntentStore, order: Intent) -> tuple[float | None, float | None]:
    for _, kind, detail in store.event_rows(order.client_order_id):
        if kind == "intent":
            return detail.get("bid"), detail.get("ask")
    return None, None


def _avg(orders: list[Intent]) -> tuple[int, float]:
    q = sum(o.qty_filled for o in orders)
    cost = sum(o.qty_filled * (o.avg_fill_price or 0.0) for o in orders)
    return q, (cost / q if q else 0.0)


def broker_fills(store: IntentStore, account: str, mult: int) -> list[Fill]:
    out = []
    for o in store.all_for(account):
        if o.qty_filled <= 0:
            continue
        price = o.avg_fill_price or 0.0
        sign = -1 if o.side == "buy" else 1
        out.append(
            Fill(
                o.client_order_id, o.candidate_id, o.purpose.value, o.side, o.symbol,
                o.qty_filled, price, sign * o.qty_filled * price * mult, _fill_time(store, o),
            )
        )  # fmt: skip
    return sorted(out, key=lambda f: (f.at is None, f.at))


def _evaluated(price_side: str, bid, ask, slip_ticks: float, tick: float, buy: bool):
    """The evaluation price from a decision quote, or None if there is no usable one."""
    if buy:
        base = (
            ask
            if price_side == "ask"
            else (None if bid is None or ask is None else (bid + ask) / 2)
        )
        return None if base is None or base <= 0 else base + slip_ticks * tick
    base = bid if price_side == "bid" else (None if bid is None or ask is None else (bid + ask) / 2)
    return None if base is None or base <= 0 else max(base - slip_ticks * tick, 0.0)


def build_trades(store: IntentStore, account: str, cfg: LedgerConfig) -> list[Trade]:
    ev, mult = cfg.evaluation, cfg.evaluation.contract_multiplier
    per_contract = cfg.costs.commission_per_contract + cfg.costs.regulatory_fee_per_contract
    by_candidate: dict[str, list[Intent]] = {}
    for o in store.all_for(account):
        by_candidate.setdefault(o.candidate_id, []).append(o)
    trades = []
    for cid, orders in by_candidate.items():
        entries = [o for o in orders if o.purpose is Purpose.ENTRY and o.qty_filled > 0]
        if not entries:
            continue  # nothing bought: a decision that never became a position
        exits = [o for o in orders if o.purpose is Purpose.EXIT and o.qty_filled > 0]
        all_exit_orders = [o for o in orders if o.purpose is Purpose.EXIT]
        q_in, p_in = _avg(entries)
        q_out, p_out = _avg(exits)
        closed = min(q_in, q_out)
        flags: list[str] = []
        ebid, eask = _decision_quote(store, min(entries, key=lambda o: o.created_at))
        e_entry = _evaluated(
            ev.entry_price, ebid, eask, ev.entry_slippage_ticks, ev.tick_size, True
        )
        if e_entry is None:
            flags.append(NO_ENTRY_QUOTE)
            e_entry = p_in if ev.missing_quote == "flag_and_use_broker" else None
        e_exit = None
        if closed:
            first_exit = min(all_exit_orders, key=lambda o: o.created_at)
            xbid, xask = _decision_quote(store, first_exit)
            e_exit = _evaluated(
                ev.exit_price, xbid, xask, ev.exit_slippage_ticks, ev.tick_size, False
            )
            if e_exit is None:
                flags.append(NO_EXIT_QUOTE)
                e_exit = p_out if ev.missing_quote == "flag_and_use_broker" else None
        if closed < q_in:
            flags.append(OPEN_AT_END)
        entry_at = min(
            (t for t in (_fill_time(store, o) for o in entries) if t is not None), default=None
        )
        exit_at = max(
            (t for t in (_fill_time(store, o) for o in exits) if t is not None), default=None
        )
        occ = parse_occ(entries[0].symbol)
        broker_gross = (p_out - p_in) * closed * mult if closed else 0.0
        eval_gross = (
            ((e_exit - e_entry) * closed * mult)
            if (closed and e_entry is not None and e_exit is not None)
            else 0.0
        )
        fees = per_contract * (q_in + q_out)
        stress = {}
        for sc in cfg.stress:
            if closed and e_entry is not None and e_exit is not None:
                gross = (
                    (
                        (e_exit - sc.extra_slippage_ticks * ev.tick_size)
                        - (e_entry + sc.extra_slippage_ticks * ev.tick_size)
                    )
                    * closed
                    * mult
                )
            else:
                gross = 0.0
            stress[sc.name] = gross - fees * sc.fee_multiplier
        reasons = sorted({r for o in all_exit_orders for r in o.reasons})
        timed = entry_at is not None and exit_at is not None and closed
        trades.append(
            Trade(
                candidate_id=cid,
                strategy_id=entries[0].strategy_id,
                symbol=entries[0].symbol,
                right=occ[1] if occ else None,
                entry_at=entry_at,
                exit_at=exit_at,
                qty=q_in,
                closed_qty=closed,
                open_qty=q_in - closed,
                broker_entry=p_in,
                broker_exit=p_out if closed else None,
                eval_entry=e_entry if e_entry is not None else p_in,
                eval_exit=e_exit,
                broker_gross_usd=broker_gross,
                eval_gross_usd=eval_gross,
                fees_usd=fees,
                net_eval_usd=eval_gross - fees,
                stress_net_usd=stress,
                holding_s=(exit_at - entry_at).total_seconds() if timed else None,
                premium_usd=q_in * p_in * mult,
                exit_reasons=reasons,
                flags=flags,
            )
        )
    return sorted(trades, key=lambda t: (t.entry_at is None, t.entry_at))
