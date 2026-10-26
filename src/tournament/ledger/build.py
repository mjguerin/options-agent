"""Building the two ledgers for one account from its orders (BRD 12.3; AC-11).

The execution ledger is what the execution side recorded: each order's filled quantity and average
price, the cash those fills moved, and the positions left. In shadow mode that is the modeled
execution; only paper execution produces a broker ledger (D-79). The evaluation ledger re-prices
the same executions against a benchmark: the ask the application held when each entry was
decided, the bid when each exit was decided, a configured number of ticks worse, and the
configured per-contract costs (D-80). It is a signal-price benchmark, not an executable-fill
assumption, and it measures sensitivity of these trades only: it does not replay how other fills
would have changed buying power or later eligibility. Every competitor is priced the same way. The
two ledgers are never netted into each other: both are reported, with the difference, and a
decision with no usable quote is flagged rather than priced from thin air. Each quantity maps to
the decision that produced it, and the quote's own times, sizes and quality travel with it.
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
    entry_benchmark: list[dict] = field(default_factory=list)
    exit_benchmark: list[dict] = field(default_factory=list)
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


def _decision_quote(store: IntentStore, order: Intent) -> dict:
    """The quote and provenance recorded when this order's decision was made."""
    for _, kind, detail in store.event_rows(order.client_order_id):
        if kind == "intent" and "bid" in detail:
            return {
                "bid": detail.get("bid"),
                "ask": detail.get("ask"),
                "decision_at": detail.get("decision_at"),
                "quote": detail.get("quote") or {},
            }
    return {"bid": None, "ask": None, "decision_at": None, "quote": {}}


def _origin(store: IntentStore, order: Intent, by_id: dict[str, Intent]) -> Intent:
    """The order a decision started with: a replacement is the same decision as its parent."""
    seen = {order.client_order_id}
    while order.parent_client_order_id and order.parent_client_order_id in by_id:
        order = by_id[order.parent_client_order_id]
        if order.client_order_id in seen:
            break
        seen.add(order.client_order_id)
    return order


def _benchmarks(
    store: IntentStore, orders: list[Intent], by_id: dict[str, Intent], cfg: LedgerConfig, buy: bool
) -> tuple[list[dict], float, bool]:
    """One record per filled order: its quantity, the decision quote it maps to, the price the
    benchmark gives it. Returns (records, quantity-weighted benchmark price, any missing)."""
    ev = cfg.evaluation
    out, qty, cost, missing = [], 0, 0.0, False
    for o in sorted(orders, key=lambda o: o.created_at):
        if o.qty_filled <= 0:
            continue
        dq = _decision_quote(store, _origin(store, o, by_id))
        if buy:
            price = _evaluated(ev.entry_price, dq["bid"], dq["ask"], ev.entry_slippage_ticks,
                               ev.tick_size, True)  # fmt: skip
        else:
            price = _evaluated(ev.exit_price, dq["bid"], dq["ask"], ev.exit_slippage_ticks,
                               ev.tick_size, False)  # fmt: skip
        out.append(
            {
                "client_order_id": o.client_order_id,
                "quantity": o.qty_filled,
                "decision_at": dq["decision_at"],
                "bid": dq["bid"],
                "ask": dq["ask"],
                "benchmark_price": price,
                "quote": dq["quote"],
            }
        )
        if price is None:
            missing = True
        else:
            qty += o.qty_filled
            cost += o.qty_filled * price
    return out, (cost / qty if qty else None), missing


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


def build_trades(
    store: IntentStore, account: str, cfg: LedgerConfig, day: str | None = None
) -> list[Trade]:
    ev, mult = cfg.evaluation, cfg.evaluation.contract_multiplier
    rate = cfg.costs.rate_on(day)
    per_contract = rate.commission_per_contract + rate.regulatory_fee_per_contract
    all_orders = store.all_for(account)
    by_id = {o.client_order_id: o for o in all_orders}
    by_candidate: dict[str, list[Intent]] = {}
    for o in all_orders:
        by_candidate.setdefault(o.candidate_id, []).append(o)
    trades = []
    for cid, orders in by_candidate.items():
        entries = [o for o in orders if o.purpose is Purpose.ENTRY and o.qty_filled > 0]
        if not entries:
            continue  # nothing bought: a decision that never became a position
        exits = [o for o in orders if o.purpose is Purpose.EXIT and o.qty_filled > 0]
        q_in, p_in = _avg(entries)
        q_out, p_out = _avg(exits)
        closed = min(q_in, q_out)
        flags: list[str] = []
        entry_rec, e_entry, entry_missing = _benchmarks(store, entries, by_id, cfg, True)
        if entry_missing:
            flags.append(NO_ENTRY_QUOTE)
        if e_entry is None and ev.missing_quote == "flag_and_use_broker":
            e_entry = p_in  # flagged above: the execution price, never an invented quote
        exit_rec: list[dict] = []
        e_exit = None
        if closed:
            exit_rec, e_exit, exit_missing = _benchmarks(store, exits, by_id, cfg, False)
            if exit_missing:
                flags.append(NO_EXIT_QUOTE)
                if e_exit is None and ev.missing_quote == "flag_and_use_broker":
                    e_exit = p_out
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
        priced = closed and e_entry is not None and e_exit is not None
        eval_gross = ((e_exit - e_entry) * closed * mult) if priced else 0.0
        fees = per_contract * (q_in + q_out)
        stress = {}
        for sc in cfg.stress:
            if priced:
                sold = max(e_exit - sc.extra_slippage_ticks * ev.tick_size, 0.0)  # floor at zero
                bought = e_entry + sc.extra_slippage_ticks * ev.tick_size
                gross = (sold - bought) * closed * mult
            else:
                gross = 0.0
            stress[sc.name] = gross - fees * sc.fee_multiplier
        reasons = sorted({r for o in orders if o.purpose is Purpose.EXIT for r in o.reasons})
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
                entry_benchmark=entry_rec,
                exit_benchmark=exit_rec,
                holding_s=(exit_at - entry_at).total_seconds() if timed else None,
                premium_usd=q_in * p_in * mult,
                exit_reasons=reasons,
                flags=flags,
            )
        )
    return sorted(trades, key=lambda t: (t.entry_at is None, t.entry_at))
