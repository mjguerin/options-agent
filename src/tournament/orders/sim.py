"""The modeled broker for shadow mode (BRD 5.3 shadow, section 11 modeled execution).

It implements the same `Broker` boundary as the paper adapter, so the order manager and the
account runtime run unchanged. Fills are modeled from the live quotes: a marketable limit order
fills once it is `fill_delay_s` old, at the touch (the ask for a buy, the bid for a sell) or at
its own limit, per config. A limit that isn't marketable stays open until the quote reaches it;
a market order fills at the touch. No quote, no fill. It never invents liquidity beyond what
the quote shows... except that it ignores quoted size, which is a declared approximation.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from tournament.orders.broker import (
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from tournament.orders.config import Simulation
from tournament.orders.model import Status

Quote = tuple[float | None, float | None]  # bid, ask


class SimBroker:
    def __init__(
        self,
        quotes: Callable[[str], Quote],
        sim: Simulation,
        starting_cash: float,
        multiplier: int,
    ) -> None:
        self.quotes, self.sim, self.multiplier = quotes, sim, multiplier
        self._cash = starting_cash
        self.now: pd.Timestamp | None = None
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []

    # ---- the Broker protocol ------------------------------------------------------------
    def submit(self, req: OrderRequest) -> BrokerOrder:
        if req.client_order_id in self.orders:
            from tournament.orders.broker import BrokerReject

            raise BrokerReject("client_order_id must be unique")
        self.orders[req.client_order_id] = {
            "req": req,
            "id": f"SIM{len(self.orders) + 1}",
            "status": Status.OPEN,
            "filled": 0,
            "price": None,
            "at": self.now,
        }
        return self._view(req.client_order_id)

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
        for cid, o in self.orders.items():
            if o["status"] is Status.OPEN and o["at"] is not None:
                if (now - o["at"]).total_seconds() >= self.sim.fill_delay_s:
                    self._try_fill(cid, o)
            elif o["status"] is Status.OPEN and o["at"] is None:
                o["at"] = now

    def _try_fill(self, cid: str, o: dict) -> None:
        req: OrderRequest = o["req"]
        bid, ask = self.quotes(req.symbol)
        buy = req.side == "buy"
        touch = ask if buy else bid
        if touch is None or touch <= 0:
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
        o.update(filled=req.qty, price=price, status=Status.FILLED)
        self._cash += (-1 if buy else 1) * req.qty * price * self.multiplier
        self.fills.append({"client_order_id": cid, "qty": req.qty, "price": price, "at": self.now})

    def cash(self) -> float:
        return self._cash

    def _view(self, cid: str) -> BrokerOrder:
        o = self.orders[cid]
        return BrokerOrder(cid, o["id"], o["status"], o["req"].qty, o["filled"], o["price"])
