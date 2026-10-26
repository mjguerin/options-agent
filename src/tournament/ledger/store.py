"""The ledgers on disk, append-only (BRD 11: corrections append auditable records).

A session's ledger for an account is written once. Writing the same content again changes nothing;
writing different content (a correction, a changed assumption) appends a new revision and keeps the
old one, so no result is silently rewritten (AC-10, AC-11).
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    Text,
    func,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from tournament.db import metadata

_ID = BigInteger().with_variant(Integer, "sqlite")
ledger_sessions = Table(
    "ledger_sessions",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("experiment_id", String(64), nullable=False, index=True),
    Column("mode", String(8), nullable=False),
    Column("session_date", String(10), nullable=False),
    Column("account", String(64), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("config_hash", String(64), nullable=False),
    Column("reconciled", Boolean, nullable=False),
    Column("written_at", DateTime(timezone=True), nullable=False),
    Column("summary", Text, nullable=False),
    Column("metrics", Text, nullable=False),
    Column("reconciliation", Text, nullable=False),
)
ledger_trades = Table(
    "ledger_trades",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ledger_id", BigInteger, nullable=False, index=True),
    Column("candidate_id", String(128), nullable=False),
    Column("strategy_id", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("entry_at", DateTime(timezone=True)),
    Column("exit_at", DateTime(timezone=True)),
    Column("qty", Integer, nullable=False),
    Column("closed_qty", Integer, nullable=False),
    Column("broker_entry", Float, nullable=False),
    Column("broker_exit", Float),
    Column("eval_entry", Float, nullable=False),
    Column("eval_exit", Float),
    Column("broker_gross_usd", Float, nullable=False),
    Column("eval_gross_usd", Float, nullable=False),
    Column("fees_usd", Float, nullable=False),
    Column("net_eval_usd", Float, nullable=False),
    Column("detail", Text, nullable=False),  # reasons, flags, stress, holding time
)
ledger_fills = Table(
    "ledger_fills",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ledger_id", BigInteger, nullable=False, index=True),
    Column("client_order_id", String(128), nullable=False),
    Column("candidate_id", String(128), nullable=False),
    Column("purpose", String(8), nullable=False),
    Column("side", String(4), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("price", Float, nullable=False),
    Column("cash_usd", Float, nullable=False),
    Column("at", DateTime(timezone=True)),
)
ledger_equity = Table(
    "ledger_equity",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ledger_id", BigInteger, nullable=False, index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("realized_usd", Float),
    Column("unrealized_usd", Float),
    Column("estimated_usd", Float),
    Column("risk_usd", Float),
    Column("exposure_usd", Float),
)


def _t(v):
    return None if v is None else pd.Timestamp(v).to_pydatetime()


def write_session(
    engine: Engine, experiment_id: str, mode: str, body: dict, now: pd.Timestamp
) -> tuple[int, int, bool]:
    """(ledger id, revision, created). Identical content is a no-op."""
    s = body["summary"]
    key = (
        ledger_sessions.c.experiment_id == experiment_id,
        ledger_sessions.c.mode == mode,
        ledger_sessions.c.session_date == s["session_date"],
        ledger_sessions.c.account == s["account"],
    )
    with engine.begin() as c:
        last = c.execute(
            select(ledger_sessions.c.id, ledger_sessions.c.revision, ledger_sessions.c.content_hash)
            .where(*key)
            .order_by(ledger_sessions.c.revision.desc())
            .limit(1)
        ).first()
        if last is not None and last.content_hash == body["content_hash"]:
            return last.id, last.revision, False
        revision = 1 if last is None else last.revision + 1
        lid = c.execute(
            insert(ledger_sessions).values(
                experiment_id=experiment_id,
                mode=mode,
                session_date=s["session_date"],
                account=s["account"],
                revision=revision,
                content_hash=body["content_hash"],
                config_hash=body["config_hash"],
                reconciled=body["reconciliation"]["ok"],
                written_at=now.to_pydatetime(),
                summary=json.dumps(s, default=str),
                metrics=json.dumps(body["metrics"], default=str),
                reconciliation=json.dumps(body["reconciliation"], default=str),
            )
        ).inserted_primary_key[0]
        for t in body["trades"]:
            c.execute(
                insert(ledger_trades).values(
                    ledger_id=lid,
                    candidate_id=t["candidate_id"],
                    strategy_id=t["strategy_id"],
                    symbol=t["symbol"],
                    entry_at=_t(t["entry_at"]),
                    exit_at=_t(t["exit_at"]),
                    qty=t["qty"],
                    closed_qty=t["closed_qty"],
                    broker_entry=t["broker_entry"],
                    broker_exit=t["broker_exit"],
                    eval_entry=t["eval_entry"],
                    eval_exit=t["eval_exit"],
                    broker_gross_usd=t["broker_gross_usd"],
                    eval_gross_usd=t["eval_gross_usd"],
                    fees_usd=t["fees_usd"],
                    net_eval_usd=t["net_eval_usd"],
                    detail=json.dumps(
                        {
                            k: t[k]
                            for k in (
                                "exit_reasons",
                                "flags",
                                "stress_net_usd",
                                "holding_s",
                                "premium_usd",
                                "open_qty",
                                "right",
                            )
                        },
                        default=str,
                    ),
                )
            )
        for f in body["fills"]:
            c.execute(insert(ledger_fills).values(ledger_id=lid, **{**f, "at": _t(f["at"])}))
        for e in body["equity"]:
            c.execute(insert(ledger_equity).values(ledger_id=lid, **{**e, "at": _t(e["at"])}))
    return lid, revision, True


def latest_sessions(engine: Engine, experiment_id: str, mode: str) -> list[dict]:
    """The newest revision of every (session, account), with the summary, metrics and checks."""
    with engine.connect() as c:
        newest = (
            select(
                ledger_sessions.c.session_date,
                ledger_sessions.c.account,
                func.max(ledger_sessions.c.revision).label("rev"),
            )
            .where(ledger_sessions.c.experiment_id == experiment_id, ledger_sessions.c.mode == mode)
            .group_by(ledger_sessions.c.session_date, ledger_sessions.c.account)
            .subquery()
        )
        rows = c.execute(
            select(ledger_sessions)
            .join(
                newest,
                (ledger_sessions.c.session_date == newest.c.session_date)
                & (ledger_sessions.c.account == newest.c.account)
                & (ledger_sessions.c.revision == newest.c.rev),
            )
            .where(ledger_sessions.c.experiment_id == experiment_id, ledger_sessions.c.mode == mode)
            .order_by(ledger_sessions.c.session_date, ledger_sessions.c.account)
        ).all()
    return [
        {
            "session_date": r.session_date,
            "account": r.account,
            "revision": r.revision,
            "reconciled": r.reconciled,
            "summary": json.loads(r.summary),
            "metrics": json.loads(r.metrics),
            "reconciliation": json.loads(r.reconciliation),
        }
        for r in rows
    ]
