"""The control API (BRD 14; NFR-08, NFR-14, RK-02, AG-07).

Reads are open to the `viewer` and `owner` tokens. Controls need the `owner` token. An
exposure-changing control takes two requests: the first returns a one-time confirmation that
names exactly what will happen, the second presents it. Every request that matters, accepted or
refused, is written to the audit table before the answer goes back. The API never touches an
order: it queues a command, and the engine decides whether to apply it.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from tournament.accounts.manifest import STRATEGIES
from tournament.alerts.store import AlertStore
from tournament.api.config import ApiConfig
from tournament.api.store import ControlStore
from tournament.live.subscriptions import parse_occ
from tournament.secrets import ApiTokens

OWNER, VIEWER = "owner", "viewer"
KNOWN_ACCOUNTS = frozenset(f"acct:{s}" for s in STRATEGIES)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    scope: str
    account: str | None = None
    symbol: str | None = None
    confirm_id: str | None = None


def read_state(path: Path, max_age_s: float, now: pd.Timestamp) -> tuple[dict | None, bool]:
    """The engine's last published state and whether it is fresh enough to rely on."""
    try:
        state = json.loads(path.read_text())
        age = (now - pd.Timestamp(state["at"])).total_seconds()
    except (OSError, ValueError, KeyError):
        return None, False
    return state, age <= max_age_s


def create_app(
    store: ControlStore,
    cfg: ApiConfig,
    tokens: ApiTokens,
    state_file: Path,
    alerts: AlertStore | None = None,
    clock: Callable[[], pd.Timestamp] = lambda: pd.Timestamp(datetime.now().astimezone()),
) -> FastAPI:
    app = FastAPI(title="Options tournament control API", docs_url=None, redoc_url=None)

    def now() -> pd.Timestamp:
        return clock().tz_convert("UTC")

    def role_of(request: Request) -> str:
        header = request.headers.get("authorization", "")
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        # constant-time comparison, against both tokens, so timing says nothing about either
        is_owner = hmac.compare_digest(presented.encode(), tokens.owner.encode())
        is_viewer = hmac.compare_digest(presented.encode(), tokens.viewer.encode())
        source = request.client.host if request.client else None
        if presented and is_owner:
            return OWNER
        if presented and is_viewer:
            return VIEWER
        store.audit(
            now(), "anonymous", "UNAUTHENTICATED", f"{request.method} {request.url.path}", source
        )
        raise HTTPException(401, "missing or invalid token")

    def viewer_or_owner(request: Request) -> str:
        return role_of(request)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/v1/status")
    def status(role: str = Depends(viewer_or_owner)) -> dict:
        t = now()
        state, fresh = read_state(state_file, cfg.state.max_age_s, t)
        pending = store.pending(t)
        return {
            "role": role,
            "engine_reporting": fresh,
            "state": state,
            "pending_commands": [
                {"id": c.id, "command": c.command, "scope": c.scope, "account": c.account}
                for c in pending
            ],
            "open_incidents": [
                {"kind": i.kind, "severity": i.severity, "account": i.account, "detail": i.detail,
                 "opened_at": i.opened_at.isoformat()}
                for i in (alerts.open_incidents() if alerts else [])
            ],
        }  # fmt: skip

    @app.get("/v1/incidents")
    def incidents(limit: int = 50, role: str = Depends(viewer_or_owner)) -> dict:
        rows = list(reversed(alerts.all_incidents()))[: min(max(limit, 1), 500)] if alerts else []
        return {
            "incidents": [
                {
                    "kind": i.kind,
                    "severity": i.severity,
                    "account": i.account,
                    "detail": i.detail,
                    "opened_at": i.opened_at.isoformat(),
                    "closed_at": i.closed_at.isoformat() if i.closed_at else None,
                }
                for i in rows
            ]
        }

    @app.get("/v1/audit")
    def audit(limit: int = 0, role: str = Depends(viewer_or_owner)) -> dict:
        n = min(limit or cfg.commands.audit_page_size, cfg.commands.audit_page_size)
        return {"actions": store.recent_actions(n)}

    @app.get("/v1/commands/{command_id}")
    def command(command_id: str, role: str = Depends(viewer_or_owner)) -> dict:
        c = store.get(command_id)
        if c is None:
            raise HTTPException(404, "no such command")
        return {
            "id": c.id,
            "command": c.command,
            "scope": c.scope,
            "account": c.account,
            "symbol": c.symbol,
            "status": c.status,
            "requested_at": c.requested_at.isoformat(),
            "applied_at": c.applied_at.isoformat() if c.applied_at else None,
            "result": c.result,
        }

    @app.post("/v1/controls", status_code=202)
    def control(
        req: ControlRequest, request: Request, role: str = Depends(viewer_or_owner)
    ) -> dict:
        t = now()
        source = request.client.host if request.client else None

        def refuse(code: int, outcome: str, detail: str):
            store.audit(t, role, outcome, detail, source, req.command, req.scope, req.account)
            raise HTTPException(code, detail)

        if role != OWNER:
            refuse(403, "DENIED", "controls need the owner token")
        spec = cfg.controls.get(req.command)
        if spec is None:
            refuse(422, "INVALID", f"unknown control {req.command!r}")
        if req.scope not in spec.scopes:
            refuse(422, "INVALID", f"{req.command} does not apply to scope {req.scope!r}")
        if req.scope == "account" and req.account not in KNOWN_ACCOUNTS:
            refuse(422, "INVALID", f"unknown account {req.account!r}")
        if req.scope == "global" and req.account is not None:
            refuse(422, "INVALID", "a global control takes no account")
        if spec.needs_symbol and (req.symbol is None or parse_occ(req.symbol) is None):
            refuse(422, "INVALID", f"{req.command} needs one SPY option symbol")
        if not spec.needs_symbol and req.symbol is not None:
            refuse(422, "INVALID", f"{req.command} takes no symbol")

        key = (OWNER, req.command, req.scope, req.account, req.symbol)
        confirm_id = req.confirm_id
        if spec.confirm:
            if confirm_id is None:
                cid, expires = store.issue_confirmation(
                    t,
                    cfg.commands.confirmation_ttl_s,
                    OWNER,
                    req.command,
                    req.scope,
                    req.account,
                    req.symbol,
                )
                state, fresh = read_state(state_file, cfg.state.max_age_s, t)
                store.audit(
                    t,
                    role,
                    "CONFIRMATION_ISSUED",
                    cid[:8],
                    source,
                    req.command,
                    req.scope,
                    req.account,
                )
                return {
                    "needs_confirmation": True,
                    "confirm_id": cid,
                    "expires_at": expires.isoformat(),
                    "will_do": spec.summary,
                    "applies_to": {
                        "scope": req.scope,
                        "account": req.account,
                        "symbol": req.symbol,
                    },
                    "engine_reporting": fresh,
                    "positions": _positions(state, req.scope, req.account),
                }
            ok, why = store.consume_confirmation(
                t, confirm_id, key[0], req.command, req.scope, req.account, req.symbol
            )
            if not ok:
                refuse(409, "CONFIRMATION_REFUSED", why)
        command_id = store.enqueue(
            t,
            cfg.commands.ttl_s,
            OWNER,
            req.command,
            req.scope,
            req.account,
            req.symbol,
            confirm_id,
        )
        store.audit(
            t,
            role,
            "ACCEPTED",
            "queued for the engine",
            source,
            req.command,
            req.scope,
            req.account,
            command_id,
        )
        _, fresh = read_state(state_file, cfg.state.max_age_s, t)
        return {
            "command_id": command_id,
            "status": "PENDING",
            "engine_reporting": fresh,
            "note": None
            if fresh
            else "the engine is not reporting; the command waits until it expires",
        }

    return app


def _positions(state: dict | None, scope: str, account: str | None) -> dict:
    """The held positions the confirmation covers, so the owner sees what is at stake."""
    if not state:
        return {}
    accounts = state.get("accounts", {})
    names = [account] if scope == "account" else list(accounts)
    return {a: accounts.get(a, {}).get("positions", {}) for a in names if a in accounts}
