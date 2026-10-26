"""Incidents and the alert outbox (BRD 10.5, 11): the hand-off between the engine and the bot.

The engine only writes here; it never waits on Telegram. An incident is opened once per
`dedupe_key` while it stays open, so a flapping feed doesn't produce a message per flap. The
outbox holds the rendered text, so a message is exactly what was decided at the time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

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
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from tournament.alerts.config import AlertsConfig
from tournament.db import metadata

_ID = BigInteger().with_variant(Integer, "sqlite")
incidents = Table(
    "incidents",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("dedupe_key", String(160), nullable=False, index=True),
    Column("kind", String(48), nullable=False),
    Column("severity", String(8), nullable=False),
    Column("component", String(32), nullable=False),
    Column("account", String(64)),
    Column("detail", Text, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("last_alert_at", DateTime(timezone=True)),
)
alert_outbox = Table(
    "alert_outbox",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("incident_id", BigInteger),
    Column("event", String(12), nullable=False),  # OPENED | REPEAT | RESOLVED | INFO | NOTE
    Column("severity", String(8), nullable=False),
    Column("text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("dropped", String(24)),  # EXPIRED | BELOW_MIN_SEVERITY
    Column("rank", Float, nullable=False),
)


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


@dataclass(frozen=True)
class Incident:
    id: int
    dedupe_key: str
    kind: str
    severity: str
    component: str
    account: str | None
    detail: str
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp | None
    last_alert_at: pd.Timestamp | None


@dataclass(frozen=True)
class Outbox:
    id: int
    incident_id: int | None
    event: str
    severity: str
    text: str
    created_at: pd.Timestamp
    attempts: int
    next_attempt_at: pd.Timestamp | None


class AlertStore:
    def __init__(self, engine: Engine, cfg: AlertsConfig) -> None:
        self.engine, self.cfg = engine, cfg

    # ---- writes (the engine and the bot's watchdogs) --------------------------------------
    def open(
        self,
        now: pd.Timestamp,
        kind: str,
        component: str,
        detail: str = "",
        account: str | None = None,
        key: str | None = None,
    ) -> bool:
        """Open an incident unless one with this key is already open. True if it is new."""
        k = self.cfg.kinds[kind]
        key = key or (f"{kind}:{account}" if account else kind)
        with self.engine.begin() as c:
            if c.execute(
                select(incidents.c.id).where(
                    incidents.c.dedupe_key == key, incidents.c.closed_at.is_(None)
                )
            ).first():
                return False
            res = c.execute(
                insert(incidents).values(
                    dedupe_key=key,
                    kind=kind,
                    severity=k.severity,
                    component=component,
                    account=account,
                    detail=detail,
                    opened_at=now.to_pydatetime(),
                    last_alert_at=now.to_pydatetime(),
                )
            )
            iid = res.inserted_primary_key[0]
            text = k.open.format(account=account or "", detail=detail)
            self._outbox(c, now, iid, "OPENED", k.severity, text)
        return True

    def resolve(self, now: pd.Timestamp, key: str, kind: str | None = None) -> bool:
        with self.engine.begin() as c:
            row = c.execute(
                select(incidents).where(
                    incidents.c.dedupe_key == key, incidents.c.closed_at.is_(None)
                )
            ).first()
            if row is None:
                return False
            c.execute(
                update(incidents)
                .where(incidents.c.id == row.id)
                .values(closed_at=now.to_pydatetime())
            )
            k = self.cfg.kinds[kind or row.kind]
            if k.resolved:
                text = k.resolved.format(account=row.account or "", detail=row.detail)
                self._outbox(c, now, row.id, "RESOLVED", "info", text)
        return True

    def notify(self, now: pd.Timestamp, kind: str, detail: str, account: str | None = None) -> None:
        """A one-off message that is not an incident (the session summary)."""
        k = self.cfg.kinds[kind]
        with self.engine.begin() as c:
            self._outbox(
                c,
                now,
                None,
                "INFO",
                k.severity,
                k.open.format(account=account or "", detail=detail),
            )

    def note(self, now: pd.Timestamp, severity: str, text: str) -> None:
        with self.engine.begin() as c:
            self._outbox(c, now, None, "NOTE", severity, text)

    def _outbox(self, c, now, incident_id, event, severity, text) -> None:
        c.execute(
            insert(alert_outbox).values(
                incident_id=incident_id,
                event=event,
                severity=severity,
                text=text,
                created_at=now.to_pydatetime(),
                attempts=0,
                next_attempt_at=now.to_pydatetime(),
                rank=float(["info", "warning", "critical"].index(severity)),
            )
        )

    # ---- the bot's side ------------------------------------------------------------------
    def due(self, now: pd.Timestamp, limit: int) -> list[Outbox]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(alert_outbox)
                .where(alert_outbox.c.sent_at.is_(None), alert_outbox.c.dropped.is_(None))
                .order_by(alert_outbox.c.rank.desc(), alert_outbox.c.id)
            ).all()
        out = [self._outbox_row(r) for r in rows]
        return [o for o in out if o.next_attempt_at is None or o.next_attempt_at <= now][:limit]

    def mark_sent(self, now: pd.Timestamp, oid: int) -> None:
        with self.engine.begin() as c:
            c.execute(
                update(alert_outbox)
                .where(alert_outbox.c.id == oid)
                .values(sent_at=now.to_pydatetime())
            )

    def mark_failed(self, oid: int, attempts: int, next_at: pd.Timestamp, error: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                update(alert_outbox)
                .where(alert_outbox.c.id == oid)
                .values(
                    attempts=attempts,
                    next_attempt_at=next_at.to_pydatetime(),
                    last_error=error[:500],
                )
            )

    def drop(self, oid: int, reason: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(alert_outbox).where(alert_outbox.c.id == oid).values(dropped=reason))

    def sent_since(self, since: pd.Timestamp, severity: str | None = None) -> int:
        q = select(alert_outbox.c.id).where(alert_outbox.c.sent_at >= since.to_pydatetime())
        if severity is not None:
            q = q.where(alert_outbox.c.severity == severity)
        with self.engine.connect() as c:
            return len(c.execute(q).all())

    def open_incidents(self) -> list[Incident]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(incidents).where(incidents.c.closed_at.is_(None)).order_by(incidents.c.id)
            ).all()
        return [self._incident(r) for r in rows]

    def repeat(self, now: pd.Timestamp, inc: Incident) -> None:
        k = self.cfg.kinds[inc.kind]
        with self.engine.begin() as c:
            c.execute(
                update(incidents)
                .where(incidents.c.id == inc.id)
                .values(last_alert_at=now.to_pydatetime())
            )
            self._outbox(
                c,
                now,
                inc.id,
                "REPEAT",
                inc.severity,
                "Still open: " + k.open.format(account=inc.account or "", detail=inc.detail),
            )

    def all_incidents(self) -> list[Incident]:
        with self.engine.connect() as c:
            return [
                self._incident(r)
                for r in c.execute(select(incidents).order_by(incidents.c.id)).all()
            ]

    # ---- mapping -------------------------------------------------------------------------
    @staticmethod
    def _incident(r) -> Incident:
        return Incident(r.id, r.dedupe_key, r.kind, r.severity, r.component, r.account, r.detail,
                        _ts(r.opened_at), _ts(r.closed_at), _ts(r.last_alert_at))  # fmt: skip

    @staticmethod
    def _outbox_row(r) -> Outbox:
        return Outbox(
            r.id,
            r.incident_id,
            r.event,
            r.severity,
            r.text,
            _ts(r.created_at),
            r.attempts,
            _ts(r.next_attempt_at),
        )


def detail_json(**kw) -> str:
    return json.dumps(kw, sort_keys=True, default=str)
