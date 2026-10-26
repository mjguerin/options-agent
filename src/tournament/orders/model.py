"""Order records (OM-02, OM-04, OM-07): intent, status and the separate quantities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import pandas as pd


class Status(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # persisted, not yet sent (OM-02)
    SUBMITTED = "SUBMITTED"  # sent, no acknowledgement yet
    UNCERTAIN = "UNCERTAIN"  # sent, outcome unknown: reconcile before anything else (OM-03)
    OPEN = "OPEN"  # acknowledged and working
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    NOT_FOUND = "NOT_FOUND"  # the broker has no such client order ID after reconciliation


ACTIVE = frozenset(
    {
        Status.PENDING_SUBMIT,
        Status.SUBMITTED,
        Status.UNCERTAIN,
        Status.OPEN,
        Status.PARTIALLY_FILLED,
        Status.CANCEL_REQUESTED,
    }
)
TERMINAL = frozenset(set(Status) - ACTIVE)


class Purpose(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True)
class Intent:
    client_order_id: str
    account_binding: str
    candidate_id: str
    strategy_id: str
    purpose: Purpose
    side: str  # buy | sell
    symbol: str
    qty_intended: int
    order_type: str  # limit | market
    limit_price: float | None
    time_in_force: str
    status: Status
    created_at: pd.Timestamp
    replacement: int = 0  # 0 = the first order of the chain
    parent_client_order_id: str | None = None
    broker_order_id: str | None = None
    qty_submitted: int = 0
    qty_filled: int = 0
    avg_fill_price: float | None = None
    submitted_at: pd.Timestamp | None = None
    reasons: tuple[str, ...] = ()  # exit reasons that asked for this order (EX-07)
    last_error: str | None = None

    @property
    def remaining(self) -> int:
        return max(self.qty_intended - self.qty_filled, 0)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def with_(self, **changes) -> Intent:
        return replace(self, **changes)
