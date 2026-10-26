"""A scriptable fake broker for the order manager tests."""

from tournament.orders.broker import BrokerOrder, BrokerPosition, BrokerReject, BrokerUncertain
from tournament.orders.model import Status


class FakeBroker:
    def __init__(self):
        self.orders: dict[str, dict] = {}  # client id -> state
        self.submits: list = []  # every submit attempt, in order
        self.cancels: list[str] = []
        self.plan_submit: list[str] = []  # per attempt: ok | reject | lost | late
        self.unreachable = False  # lookups raise
        self.fill_on_cancel: int | None = None  # a fill that beats the cancel
        self.extra_positions: dict[str, int] = {}

    # ---- the Broker protocol ----
    def submit(self, req):
        self.submits.append(req)
        mode = self.plan_submit.pop(0) if self.plan_submit else "ok"
        if mode == "reject":
            raise BrokerReject("insufficient buying power")
        if req.client_order_id in self.orders and mode == "ok":
            raise BrokerReject("client_order_id must be unique")
        if mode == "lost":  # the request never arrived
            raise BrokerUncertain("timeout")
        self.orders[req.client_order_id] = {
            "req": req,
            "id": f"B{len(self.orders) + 1}",
            "status": Status.OPEN,
            "filled": 0,
            "price": None,
        }
        if mode == "late":  # arrived, but the reply was lost
            raise BrokerUncertain("timeout")
        return self._view(req.client_order_id)

    def get_by_client_id(self, client_order_id):
        if self.unreachable:
            raise BrokerUncertain("unreachable")
        return self._view(client_order_id) if client_order_id in self.orders else None

    def cancel(self, broker_order_id):
        for cid, o in self.orders.items():
            if o["id"] == broker_order_id:
                self.cancels.append(cid)
                if self.fill_on_cancel is not None:
                    self.fill(cid, self.fill_on_cancel, o["req"].limit_price or 1.0)
                    if o["status"] is Status.FILLED:
                        return
                if o["status"] in (Status.OPEN, Status.PARTIALLY_FILLED):
                    o["status"] = Status.CANCELED

    def positions(self):
        held: dict[str, int] = dict(self.extra_positions)
        for o in self.orders.values():
            sign = 1 if o["req"].side == "buy" else -1
            held[o["req"].symbol] = held.get(o["req"].symbol, 0) + sign * o["filled"]
        return [BrokerPosition(s, q) for s, q in held.items() if q]

    # ---- test controls ----
    def fill(self, client_order_id, qty, price):
        o = self.orders[client_order_id]
        o["filled"] = min(o["filled"] + qty, o["req"].qty)
        o["price"] = price
        o["status"] = Status.FILLED if o["filled"] >= o["req"].qty else Status.PARTIALLY_FILLED

    def _view(self, cid):
        o = self.orders[cid]
        return BrokerOrder(cid, o["id"], o["status"], o["req"].qty, o["filled"], o["price"])
