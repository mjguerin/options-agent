"""The broker boundary (OM-01): the one place orders leave the system.

`Broker` is what the order manager needs; `AlpacaPaperBroker` implements it over the paper
trading API (never live: `paper_trading_client` hard-codes paper). Tests use a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from tournament.adapters.alpaca import paper_trading_client
from tournament.orders.model import Status
from tournament.secrets import AlpacaCredentials


class BrokerReject(RuntimeError):
    """The broker refused the order outright (e.g. insufficient buying power, duplicate ID)."""


class BrokerUncertain(RuntimeError):
    """No usable reply (timeout, dropped connection): the order may or may not exist."""


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str  # buy | sell
    qty: int
    order_type: str  # limit | market
    limit_price: float | None
    time_in_force: str


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str
    status: Status
    qty: int
    filled_qty: int
    filled_avg_price: float | None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int


class Broker(Protocol):
    def submit(self, req: OrderRequest) -> BrokerOrder: ...
    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def positions(self) -> list[BrokerPosition]: ...


# Alpaca order states mapped to ours. Unknown states are treated as still working, never as
# done: guessing "done" could leave an exposed position unmanaged.
_STATUS = {
    "new": Status.OPEN,
    "accepted": Status.OPEN,
    "pending_new": Status.OPEN,
    "accepted_for_bidding": Status.OPEN,
    "held": Status.OPEN,
    "calculated": Status.OPEN,
    "pending_replace": Status.OPEN,
    "pending_review": Status.OPEN,
    "partially_filled": Status.PARTIALLY_FILLED,
    "pending_cancel": Status.CANCEL_REQUESTED,
    "filled": Status.FILLED,
    "canceled": Status.CANCELED,
    "replaced": Status.CANCELED,
    "done_for_day": Status.EXPIRED,
    "expired": Status.EXPIRED,
    "rejected": Status.REJECTED,
    "suspended": Status.REJECTED,
    "stopped": Status.REJECTED,
}


def map_status(alpaca_status: str) -> Status:
    return _STATUS.get(str(alpaca_status).lower(), Status.OPEN)


class AlpacaPaperBroker:
    def __init__(self, creds: AlpacaCredentials) -> None:
        self._client = paper_trading_client(creds)

    @staticmethod
    def _order(o) -> BrokerOrder:
        status = getattr(o.status, "value", o.status)
        price = getattr(o, "filled_avg_price", None)
        return BrokerOrder(
            o.client_order_id,
            str(o.id),
            map_status(status),
            int(float(o.qty)),
            int(float(o.filled_qty or 0)),
            None if price is None else float(price),
        )

    def submit(self, req: OrderRequest) -> BrokerOrder:
        side = OrderSide.BUY if req.side == "buy" else OrderSide.SELL
        tif = TimeInForce(req.time_in_force)
        if req.order_type == "limit":
            order = LimitOrderRequest(
                symbol=req.symbol,
                qty=req.qty,
                side=side,
                time_in_force=tif,
                limit_price=req.limit_price,
                client_order_id=req.client_order_id,
            )
        else:
            order = MarketOrderRequest(
                symbol=req.symbol,
                qty=req.qty,
                side=side,
                time_in_force=tif,
                client_order_id=req.client_order_id,
            )
        try:
            return self._order(self._client.submit_order(order))
        except APIError as exc:
            code = getattr(exc, "status_code", None)
            if code is not None and 400 <= code < 500:
                raise BrokerReject(str(exc)) from exc
            raise BrokerUncertain(str(exc)) from exc
        except (OSError, TimeoutError) as exc:
            raise BrokerUncertain(str(exc)) from exc

    def get_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return self._order(self._client.get_order_by_client_id(client_order_id))
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise BrokerUncertain(str(exc)) from exc
        except (OSError, TimeoutError) as exc:
            raise BrokerUncertain(str(exc)) from exc

    def cancel(self, broker_order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(broker_order_id)
        except APIError as exc:
            # 404/422: already filled, cancelled or being cancelled: the next lookup tells us
            if getattr(exc, "status_code", None) not in (404, 422):
                raise BrokerUncertain(str(exc)) from exc
        except (OSError, TimeoutError) as exc:
            raise BrokerUncertain(str(exc)) from exc

    def positions(self) -> list[BrokerPosition]:
        try:
            return [
                BrokerPosition(p.symbol, int(float(p.qty)))
                for p in self._client.get_all_positions()
            ]
        except (APIError, OSError, TimeoutError) as exc:
            raise BrokerUncertain(str(exc)) from exc
