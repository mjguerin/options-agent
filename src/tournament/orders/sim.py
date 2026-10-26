"""The modeled broker for shadow mode (BRD 5.3 shadow, section 11 modeled execution; D-66).

It implements the same `Broker` boundary as the paper adapter, so the order manager and the
account runtime run unchanged. A fill is a model, not evidence of what a broker would have done:
- It happens `fill_delay_s` after submission, using the latest usable quote we held at exactly that
  simulated time. A quote that arrived later is never used for that attempt.
- A buy fills only if the ask is at or below the order's limit, a sell only if the bid is at or
  above it, at the touch or at the limit per config; a market order fills at the touch.
- The fill never exceeds the size quoted at the touch (config); the rest of the order stays open
  and may fill on later quotes, until the order manager's own timeout cancels it.
- No usable quote, or a limit that isn't reached: no fill, and the order lives out its timeout.
Paper fills in Step 4 are another model of the market, not the truth this one is checked against.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from tournament.live.quotes import OptionQuote, QuoteView
from tournament.orders.broker import (
    BrokerOrder,
    BrokerPosition,
    BrokerReject,
    OrderRequest,
)
from tournament.orders.config import Simulation
from tournament.orders.model import Status


class QuoteHistory(Protocol):
    def asof(self, symbol: str, t: pd.Timestamp) -> OptionQuote | None: ...
    def usable_at(self, symbol: str, t: pd.Timestamp) -> QuoteView: ...


class SimBroker:
    def __init__(
        self, feed: QuoteHistory, sim: Simulation, starting_cash: float, multiplier: int
    ) -> None:
        self.feed, self.sim, self.multiplier = feed, sim, multiplier
        self._cash = starting_cash
        self.now: pd.Timestamp | None = None
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self._next_id = 1

    # ---- the Broker protocol ------------------------------------------------------------
    def submit(self, req: OrderRequest) -> BrokerOrder:
        if req.client_order_id in self.orders:
            raise BrokerReject("client_order_id must be unique")
        self.orders[req.client_order_id] = {
            "req": req,
            "id": self._new_id(),
            "status": Status.OPEN,
            "filled": 0,
            "notional": 0.0,
            "at": self.now,
            "tried": False,
        }
        return self._view(req.client_order_id)

    def _new_id(self) -> str:
        sid = f"SIM{self._next_id}"
        self._next_id += 1
        return sid

    def restore(self, store, account: str) -> int:
        """Rebuild the modeled broker's books from the durable order records after a restart:
        its orders, their fills, and the cash they moved. Returns the orders restored."""
        n = 0
        for i in store.all_for(account):
            sid = i.broker_order_id or ""
            if not sid.startswith("SIM") or i.client_order_id in self.orders:
                continue
            status = {Status.CANCEL_REQUESTED: Status.CANCELED}.get(i.status, i.status)
            if status in (Status.SUBMITTED, Status.UNCERTAIN):
                status = Status.OPEN
            req = OrderRequest(
                i.client_order_id, i.symbol, i.side, i.qty_intended, i.order_type,
                i.limit_price, i.time_in_force,
            )  # fmt: skip
            notional = i.qty_filled * (i.avg_fill_price or 0.0)
            self.orders[i.client_order_id] = {
                "req": req,
                "id": sid,
                "status": status,
                "filled": i.qty_filled,
                "notional": notional,
                "at": i.submitted_at,
                "tried": True,
            }
            self._cash += (-1 if i.side == "buy" else 1) * notional * self.multiplier
            self._next_id = max(self._next_id, int(sid[3:]) + 1)
            n += 1
        return n

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._view(client_order_id) if client_order_id in self.orders else None

    def cancel(self, broker_order_id: str) -> None:
        for o in self.orders.values():
            if o["id"] == broker_order_id and o["status"] in (Status.OPEN, Status.PARTIALLY_FILLED):
                o["status"] = Status.CANCELED

    def positions(self) -> list[BrokerPosition]:
        held: dict[str, int] = {}
        for o in self.orders.values():
            sign = 1 if o["req"].side == "buy" else -1
            held[o["req"].symbol] = held.get(o["req"].symbol, 0) + sign * o["filled"]
        return [BrokerPosition(s, q) for s, q in sorted(held.items()) if q]

    # ---- the model ----------------------------------------------------------------------
    def advance(self, now: pd.Timestamp) -> None:
        """Move modeled time forward and fill whatever has become fillable."""
        self.now = now
        delay = pd.Timedelta(seconds=self.sim.fill_delay_s)
        for cid, o in self.orders.items():
            if o["status"] not in (Status.OPEN, Status.PARTIALLY_FILLED):
                continue
            if o["at"] is None:  # submitted before the clock started: the clock starts it
                o["at"] = now
                continue
            first = o["at"] + delay
            if now < first:
                continue
            # the first attempt looks at the market exactly when the delay ended; later ones at now
            t = now if o["tried"] else first
            o["tried"] = True
            self._try_fill(cid, o, t, now)

    def _try_fill(self, cid: str, o: dict, t: pd.Timestamp, now: pd.Timestamp) -> None:
        req: OrderRequest = o["req"]
        symbol = req.symbol
        view = self.feed.usable_at(symbol, t)
        if not view.usable or view.quote is None:
            return
        q = view.quote
        buy = req.side == "buy"
        touch, size = (q.ask, q.ask_size) if buy else (q.bid, q.bid_size)
        if touch <= 0 or size <= 0:
            return
        if req.order_type == "limit":
            if buy and touch > req.limit_price + 1e-9:
                return
            if not buy and touch < req.limit_price - 1e-9:
                return
            use_limit = (self.sim.buy_fill if buy else self.sim.sell_fill) == "limit"
            price = req.limit_price if use_limit else touch
        else:
            price = touch
        remaining = req.qty - o["filled"]
        qty = min(remaining, size) if self.sim.size_cap == "displayed" else remaining
        if qty <= 0:
            return
        o["filled"] += qty
        o["notional"] += qty * price
        o["status"] = Status.FILLED if o["filled"] >= req.qty else Status.PARTIALLY_FILLED
        self._cash += (-1 if buy else 1) * qty * price * self.multiplier
        self.fills.append(
            {"client_order_id": cid, "qty": qty, "price": price, "at": now, "quote_at": t}
        )

    def cash(self) -> float:
        return self._cash

    def _view(self, cid: str) -> BrokerOrder:
        o = self.orders[cid]
        avg = o["notional"] / o["filled"] if o["filled"] else None
        return BrokerOrder(cid, o["id"], o["status"], o["req"].qty, o["filled"], avg)
