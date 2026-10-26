"""Commands, confirmations and the audit trail (BRD 14: durable audit records).

The API writes commands here; the engine reads and applies them. A confirmation is one-time,
short-lived and bound to the exact action and caller, so confirming one thing can never
authorize another. Nothing in these tables is ever updated except a command's status and a
confirmation's `used_at`: the audit table is append-only.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
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

operator_commands = Table(
    "operator_commands",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("command", String(24), nullable=False),
    Column("scope", String(8), nullable=False),
    Column("account", String(64)),
    Column("symbol", String(32)),
    Column("actor", String(16), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(10), nullable=False, index=True),
    Column("applied_at", DateTime(timezone=True)),
    Column("result", Text),
    Column("confirm_id", String(32)),
)
operator_actions = Table(
    "operator_actions",
    metadata,
    Column(
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("actor", String(16), nullable=False),
    Column("source", String(64)),
    Column("command", String(24)),
    Column("scope", String(8)),
    Column("account", String(64)),
    Column("outcome", String(24), nullable=False),
    Column("detail", Text, nullable=False),
    Column("command_id", String(32)),
)
control_confirmations = Table(
    "control_confirmations",
    metadata,
    Column("confirm_id", String(32), primary_key=True),
    Column("command", String(24), nullable=False),
    Column("scope", String(8), nullable=False),
    Column("account", String(64)),
    Column("symbol", String(32)),
    Column("actor", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True)),
)

PENDING, APPLIED, REJECTED, EXPIRED = "PENDING", "APPLIED", "REJECTED", "EXPIRED"


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


@dataclass(frozen=True)
class Command:
    id: str
    command: str
    scope: str
    account: str | None
    symbol: str | None
    actor: str
    requested_at: pd.Timestamp
    expires_at: pd.Timestamp
    status: str
    applied_at: pd.Timestamp | None
    result: dict | None


class ControlStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ---- audit (append-only) -----------------------------------------------------------------
    def audit(
        self,
        now: pd.Timestamp,
        actor: str,
        outcome: str,
        detail: str = "",
        source: str | None = None,
        command: str | None = None,
        scope: str | None = None,
        account: str | None = None,
        command_id: str | None = None,
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(operator_actions).values(
                    at=now.to_pydatetime(),
                    actor=actor,
                    source=source,
                    command=command,
                    scope=scope,
                    account=account,
                    outcome=outcome,
                    detail=detail,
                    command_id=command_id,
                )
            )

    def recent_actions(self, limit: int) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(operator_actions).order_by(operator_actions.c.id.desc()).limit(limit)
            ).all()
        return [
            {
                "id": r.id,
                "at": _ts(r.at).isoformat(),
                "actor": r.actor,
                "source": r.source,
                "command": r.command,
                "scope": r.scope,
                "account": r.account,
                "outcome": r.outcome,
                "detail": r.detail,
                "command_id": r.command_id,
            }
            for r in rows
        ]

    # ---- confirmations ---------------------------------------------------------------------
    def issue_confirmation(
        self,
        now: pd.Timestamp,
        ttl_s: float,
        actor: str,
        command: str,
        scope: str,
        account: str | None,
        symbol: str | None,
    ) -> tuple[str, pd.Timestamp]:
        cid = secrets.token_hex(16)
        expires = now + pd.Timedelta(seconds=ttl_s)
        with self.engine.begin() as c:
            c.execute(
                insert(control_confirmations).values(
                    confirm_id=cid,
                    command=command,
                    scope=scope,
                    account=account,
                    symbol=symbol,
                    actor=actor,
                    created_at=now.to_pydatetime(),
                    expires_at=expires.to_pydatetime(),
                )
            )
        return cid, expires

    def consume_confirmation(
        self,
        now: pd.Timestamp,
        confirm_id: str,
        actor: str,
        command: str,
        scope: str,
        account: str | None,
        symbol: str | None,
    ) -> tuple[bool, str]:
        with self.engine.begin() as c:
            row = c.execute(
                select(control_confirmations).where(
                    control_confirmations.c.confirm_id == confirm_id
                )
            ).first()
            if row is None:
                return False, "unknown confirmation"
            if row.used_at is not None:
                return False, "confirmation already used"
            if _ts(row.expires_at) < now:
                return False, "confirmation expired"
            same = (row.command, row.scope, row.account, row.symbol, row.actor) == (
                command,
                scope,
                account,
                symbol,
                actor,
            )
            if not same:
                return False, "confirmation is for a different action"
            # one use only, even if two requests race: the UPDATE succeeds for exactly one
            res = c.execute(
                update(control_confirmations)
                .where(
                    control_confirmations.c.confirm_id == confirm_id,
                    control_confirmations.c.used_at.is_(None),
                )
                .values(used_at=now.to_pydatetime())
            )
            if res.rowcount != 1:
                return False, "confirmation already used"
        return True, ""

    # ---- the command queue -------------------------------------------------------------------
    def enqueue(
        self,
        now: pd.Timestamp,
        ttl_s: float,
        actor: str,
        command: str,
        scope: str,
        account: str | None,
        symbol: str | None,
        confirm_id: str | None,
    ) -> str:
        cid = secrets.token_hex(16)
        with self.engine.begin() as c:
            c.execute(
                insert(operator_commands).values(
                    id=cid,
                    command=command,
                    scope=scope,
                    account=account,
                    symbol=symbol,
                    actor=actor,
                    requested_at=now.to_pydatetime(),
                    expires_at=(now + pd.Timedelta(seconds=ttl_s)).to_pydatetime(),
                    status=PENDING,
                    confirm_id=confirm_id,
                )
            )
        return cid

    def pending(self, now: pd.Timestamp) -> list[Command]:
        """What the engine should apply now, oldest first. Expired ones are marked and skipped."""
        with self.engine.begin() as c:
            rows = c.execute(
                select(operator_commands)
                .where(operator_commands.c.status == PENDING)
                .order_by(operator_commands.c.requested_at, operator_commands.c.id)
            ).all()
            live = []
            for r in rows:
                if _ts(r.expires_at) < now:
                    c.execute(
                        update(operator_commands)
                        .where(operator_commands.c.id == r.id)
                        .values(status=EXPIRED, applied_at=now.to_pydatetime())
                    )
                else:
                    live.append(r)
        return [self._command(r) for r in live]

    def finish(self, now: pd.Timestamp, command_id: str, status: str, result: dict) -> None:
        with self.engine.begin() as c:
            c.execute(
                update(operator_commands)
                .where(operator_commands.c.id == command_id)
                .values(status=status, applied_at=now.to_pydatetime(), result=json.dumps(result))
            )

    def get(self, command_id: str) -> Command | None:
        with self.engine.connect() as c:
            r = c.execute(
                select(operator_commands).where(operator_commands.c.id == command_id)
            ).first()
        return None if r is None else self._command(r)

    @staticmethod
    def _command(r) -> Command:
        return Command(
            r.id,
            r.command,
            r.scope,
            r.account,
            r.symbol,
            r.actor,
            _ts(r.requested_at),
            _ts(r.expires_at),
            r.status,
            _ts(r.applied_at),
            None if r.result is None else json.loads(r.result),
        )
