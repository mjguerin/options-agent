"""Atomic risk reservation (RK-03, RK-04, FR-07, AC-04, AC-06).

Before an entry order exists, its premium is claimed against the account's limits in one
transaction that locks the account's row, so two candidates cannot both pass the check and
together exceed it. The claim covers pending orders, not only filled positions (FR-07). Once
orders exist the claim is recomputed from them after every fill, partial fill, cancel, reject
and reconciliation (RK-04): premium actually held plus premium still pending. A claim that
never became an order lapses after `reservation_ttl_s`; a flat, finished one is released.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from tournament.db import metadata
from tournament.orders.model import ACTIVE, Purpose
from tournament.orders.store import IntentStore
from tournament.risk.config import Limits

risk_accounts = Table(
    "risk_accounts", metadata, Column("account", String(64), primary_key=True)
)  # one row per account: the row the reservation transaction locks
risk_reservations = Table(
    "risk_reservations",
    metadata,
    Column("reservation_id", String(160), primary_key=True),
    Column("account", String(64), nullable=False, index=True),
    Column("candidate_id", String(128), nullable=False),
    Column("amount_usd", Float, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("status", String(8), nullable=False, index=True),  # HELD | RELEASED
    Column("release_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
engine_sessions = Table(
    "engine_sessions",
    metadata,
    Column("session_id", String(64), primary_key=True),
    Column("experiment_id", String(64), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("manifest_hash", String(64), nullable=False),
    Column("commit_sha", String(40)),
    Column("started_at", DateTime(timezone=True), nullable=False),
)
EXPOSURE_LIMIT, MAX_POSITIONS = "EXPOSURE_LIMIT", "MAX_POSITIONS"


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    account: str
    candidate_id: str
    amount_usd: float
    qty: int
    status: str


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    reasons: tuple[str, ...]
    reservation: Reservation | None


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class ReservationLedger:
    def __init__(self, engine: Engine, limits: Limits, multiplier: int) -> None:
        self.engine, self.limits, self.multiplier = engine, limits, multiplier
        self._lock = threading.Lock()  # one process; the row lock covers other processes

    # ---- reserve --------------------------------------------------------------------------
    def reserve(
        self,
        now: pd.Timestamp,
        account: str,
        candidate_id: str,
        amount_usd: float,
        qty: int,
        equity_basis: float,
    ) -> ReserveResult:
        rid = f"{account}:{candidate_id}"
        with self._lock, self.engine.begin() as c:
            if (
                c.execute(select(risk_accounts).where(risk_accounts.c.account == account)).first()
                is None
            ):
                c.execute(insert(risk_accounts).values(account=account))
            c.execute(
                select(risk_accounts).where(risk_accounts.c.account == account).with_for_update()
            ).first()  # the lock: concurrent reservations for this account queue here
            held = c.execute(
                select(risk_reservations).where(
                    risk_reservations.c.account == account, risk_reservations.c.status == "HELD"
                )
            ).all()
            same = [r for r in held if r.candidate_id == candidate_id]
            if same:  # asking again is not a second claim
                return ReserveResult(True, (), self._res(same[0]))
            reasons = []
            if len(held) >= self.limits.max_open_positions:
                reasons.append(MAX_POSITIONS)
            cap = self.limits.max_open_premium_pct * equity_basis
            if sum(r.amount_usd for r in held) + amount_usd > cap + 1e-6:
                reasons.append(EXPOSURE_LIMIT)
            if reasons:
                return ReserveResult(False, tuple(reasons), None)
            c.execute(
                insert(risk_reservations).values(
                    reservation_id=rid,
                    account=account,
                    candidate_id=candidate_id,
                    amount_usd=amount_usd,
                    qty=qty,
                    status="HELD",
                    created_at=now.to_pydatetime(),
                    updated_at=now.to_pydatetime(),
                )
            )
            row = c.execute(
                select(risk_reservations).where(risk_reservations.c.reservation_id == rid)
            ).one()
            return ReserveResult(True, (), self._res(row))

    # ---- recompute (RK-04) ----------------------------------------------------------------
    def refresh(self, now: pd.Timestamp, account: str, store: IntentStore) -> list[Reservation]:
        """Re-derive every held claim from the orders. Call after any order event."""
        out = []
        with self._lock, self.engine.begin() as c:
            for r in c.execute(
                select(risk_reservations).where(
                    risk_reservations.c.account == account, risk_reservations.c.status == "HELD"
                )
            ).all():
                entries = store.chain(account, r.candidate_id, Purpose.ENTRY)
                exits = store.chain(account, r.candidate_id, Purpose.EXIT)
                if not entries:
                    age = (now - _ts(r.created_at)).total_seconds()
                    if age >= self.limits.reservation_ttl_s:
                        self._release(c, r, now, "NO_ORDER")
                    else:
                        out.append(self._res(r))
                    continue
                bought = sum(i.qty_filled for i in entries)
                sold = sum(i.qty_filled for i in exits)
                net = bought - sold
                cost = sum(i.qty_filled * (i.avg_fill_price or i.limit_price or 0) for i in entries)
                avg = cost / bought if bought else 0.0
                pending = sum(
                    i.remaining * (i.limit_price or 0) for i in entries if i.status in ACTIVE
                )
                busy = any(i.status in ACTIVE for i in (*entries, *exits))
                if net <= 0 and not busy:
                    self._release(c, r, now, "FLAT" if bought else "NO_FILL")
                    continue
                amount = (net * avg + pending) * self.multiplier
                qty = net + sum(i.remaining for i in entries if i.status in ACTIVE)
                c.execute(
                    update(risk_reservations)
                    .where(risk_reservations.c.reservation_id == r.reservation_id)
                    .values(amount_usd=amount, qty=qty, updated_at=now.to_pydatetime())
                )
                out.append(
                    Reservation(r.reservation_id, account, r.candidate_id, amount, qty, "HELD")
                )
        return out

    def release(self, now: pd.Timestamp, account: str, candidate_id: str, reason: str) -> None:
        with self._lock, self.engine.begin() as c:
            for r in c.execute(
                select(risk_reservations).where(
                    risk_reservations.c.account == account,
                    risk_reservations.c.candidate_id == candidate_id,
                    risk_reservations.c.status == "HELD",
                )
            ).all():
                self._release(c, r, now, reason)

    def held(self, account: str) -> list[Reservation]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(risk_reservations).where(
                    risk_reservations.c.account == account, risk_reservations.c.status == "HELD"
                )
            ).all()
        return [self._res(r) for r in rows]

    def exposure_usd(self, account: str) -> float:
        return sum(r.amount_usd for r in self.held(account))

    # ---- helpers --------------------------------------------------------------------------
    @staticmethod
    def _release(c, r, now: pd.Timestamp, reason: str) -> None:
        c.execute(
            update(risk_reservations)
            .where(risk_reservations.c.reservation_id == r.reservation_id)
            .values(
                status="RELEASED",
                amount_usd=0.0,
                release_reason=reason,
                updated_at=now.to_pydatetime(),
            )
        )

    @staticmethod
    def _res(r) -> Reservation:
        return Reservation(
            r.reservation_id, r.account, r.candidate_id, r.amount_usd, r.qty, r.status
        )


def record_engine_session(
    engine: Engine,
    session_id: str,
    experiment_id: str,
    mode: str,
    manifest_hash: str,
    commit_sha: str | None,
    started_at: pd.Timestamp,
) -> None:
    """Every engine session records the commit and manifest it ran under (NFR-15)."""
    with engine.begin() as c:
        c.execute(
            insert(engine_sessions).values(
                session_id=session_id,
                experiment_id=experiment_id,
                mode=mode,
                manifest_hash=manifest_hash,
                commit_sha=commit_sha,
                started_at=started_at.to_pydatetime(),
            )
        )
