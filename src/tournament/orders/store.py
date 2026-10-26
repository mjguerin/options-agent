"""Durable order intents and events (OM-02, OM-04, OM-07, NFR-04).

PostgreSQL in production, SQLite in tests. The intent row is written before anything is sent
to the broker, so a crash between the two leaves a record to reconcile, never an orphan order.
Every status change also appends an event: the row is the current state, the events are the
history.
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from tournament.db import database_url, metadata
from tournament.orders.model import ACTIVE, Intent, Purpose, Status

orders = Table(
    "orders",
    metadata,
    Column("client_order_id", String(128), primary_key=True),
    Column("account_binding", String(64), nullable=False, index=True),
    Column("candidate_id", String(128), nullable=False, index=True),
    Column("strategy_id", String(32), nullable=False),
    Column("purpose", String(8), nullable=False),
    Column("side", String(4), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("qty_intended", Integer, nullable=False),
    Column("qty_submitted", Integer, nullable=False),
    Column("qty_filled", Integer, nullable=False),
    Column("order_type", String(8), nullable=False),
    Column("limit_price", Float),
    Column("time_in_force", String(8), nullable=False),
    Column("status", String(24), nullable=False, index=True),
    Column("replacement", Integer, nullable=False),
    Column("parent_client_order_id", String(128)),
    Column("broker_order_id", String(64)),
    Column("avg_fill_price", Float),
    Column("reasons", Text, nullable=False),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("submitted_at", DateTime(timezone=True)),
)
order_events = Table(
    "order_events",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("client_order_id", String(128), nullable=False, index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("detail", Text, nullable=False),
)


def memory_engine() -> Engine:
    """An in-memory SQLite database with every table registered so far, for tests."""
    from tournament.accounts import reservations as _reservations  # noqa: F401
    from tournament.alerts import store as _alerts  # noqa: F401
    from tournament.api import store as _controls  # noqa: F401

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata.create_all(engine)
    return engine


def default_engine() -> Engine:
    return create_engine(database_url())


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class IntentStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ---- writes -------------------------------------------------------------------------
    def insert(self, i: Intent) -> None:
        with self.engine.begin() as c:
            c.execute(insert(orders).values(**self._row(i)))

    def save(self, i: Intent) -> None:
        row = self._row(i)
        del row["client_order_id"]
        with self.engine.begin() as c:
            c.execute(
                update(orders).where(orders.c.client_order_id == i.client_order_id).values(**row)
            )

    def event(self, client_order_id: str, at: pd.Timestamp, kind: str, **detail) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(order_events).values(
                    client_order_id=client_order_id,
                    at=at.to_pydatetime(),
                    kind=kind,
                    detail=json.dumps(detail, default=str, sort_keys=True),
                )
            )

    # ---- reads --------------------------------------------------------------------------
    def get(self, client_order_id: str) -> Intent | None:
        with self.engine.connect() as c:
            r = c.execute(select(orders).where(orders.c.client_order_id == client_order_id)).first()
        return None if r is None else self._intent(r)

    def chain(self, account: str, candidate_id: str, purpose: Purpose) -> list[Intent]:
        """Every order of one candidate and purpose, first order first."""
        with self.engine.connect() as c:
            rows = c.execute(
                select(orders)
                .where(
                    orders.c.account_binding == account,
                    orders.c.candidate_id == candidate_id,
                    orders.c.purpose == purpose.value,
                )
                .order_by(orders.c.replacement, orders.c.created_at)
            ).all()
        return [self._intent(r) for r in rows]

    def active(self, account: str) -> list[Intent]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(orders)
                .where(
                    orders.c.account_binding == account,
                    orders.c.status.in_([s.value for s in ACTIVE]),
                )
                .order_by(orders.c.created_at)
            ).all()
        return [self._intent(r) for r in rows]

    def all_for(self, account: str) -> list[Intent]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(orders)
                .where(orders.c.account_binding == account)
                .order_by(orders.c.created_at, orders.c.replacement)
            ).all()
        return [self._intent(r) for r in rows]

    def filled_by_symbol(self, account: str) -> dict[str, int]:
        """Net contracts per symbol from fills: bought minus sold."""
        out: dict[str, int] = {}
        with self.engine.connect() as c:
            for r in c.execute(select(orders).where(orders.c.account_binding == account)):
                sign = 1 if r.side == "buy" else -1
                out[r.symbol] = out.get(r.symbol, 0) + sign * r.qty_filled
        return {k: v for k, v in out.items() if v != 0}

    def events(self, client_order_id: str) -> list[tuple[str, dict]]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(order_events)
                .where(order_events.c.client_order_id == client_order_id)
                .order_by(order_events.c.id)
            ).all()
        return [(r.kind, json.loads(r.detail)) for r in rows]

    # ---- mapping ------------------------------------------------------------------------
    @staticmethod
    def _row(i: Intent) -> dict:
        return {
            "client_order_id": i.client_order_id,
            "account_binding": i.account_binding,
            "candidate_id": i.candidate_id,
            "strategy_id": i.strategy_id,
            "purpose": i.purpose.value,
            "side": i.side,
            "symbol": i.symbol,
            "qty_intended": i.qty_intended,
            "qty_submitted": i.qty_submitted,
            "qty_filled": i.qty_filled,
            "order_type": i.order_type,
            "limit_price": i.limit_price,
            "time_in_force": i.time_in_force,
            "status": i.status.value,
            "replacement": i.replacement,
            "parent_client_order_id": i.parent_client_order_id,
            "broker_order_id": i.broker_order_id,
            "avg_fill_price": i.avg_fill_price,
            "reasons": json.dumps(list(i.reasons)),
            "last_error": i.last_error,
            "created_at": i.created_at.to_pydatetime(),
            "submitted_at": None if i.submitted_at is None else i.submitted_at.to_pydatetime(),
        }

    @staticmethod
    def _intent(r) -> Intent:
        return Intent(
            r.client_order_id,
            r.account_binding,
            r.candidate_id,
            r.strategy_id,
            Purpose(r.purpose),
            r.side,
            r.symbol,
            r.qty_intended,
            r.order_type,
            r.limit_price,
            r.time_in_force,
            Status(r.status),
            _ts(r.created_at),
            r.replacement,
            r.parent_client_order_id,
            r.broker_order_id,
            r.qty_submitted,
            r.qty_filled,
            r.avg_fill_price,
            _ts(r.submitted_at),
            tuple(json.loads(r.reasons)),
            r.last_error,
        )
